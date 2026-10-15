"""Pinned compiler toolchains: registry, verified install, and lookup for the compile sandbox.

A toolchain is one or more upstream archives, each verified against a pinned sha256 and
extracted into its own directory. Projects name a toolchain in project.toml; the compiler
sandbox mounts that directory (and any runtime it requires) read-only.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import linuxbox, wsl

MANIFEST = ".decompnet-toolchain.json"
LINUX = "linux-x86_64"
# Parts for WSL: Linux programs packed in a format Windows can unpack (a .deb, not a .zst)
WSL_PLATFORM = "wsl-x86_64"


class ToolchainError(RuntimeError):
    pass


@dataclass
class Toolchain:
    id: str
    name: str
    license: str
    source: str
    description: str = ""
    kind: str = "compiler"  # compiler | runtime | tool
    platform: str = ""
    arch: str = ""
    cc: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)  # full command template, when compiling is more than cc + flags
    parts: list[dict] = field(default_factory=list)
    git: str = ""
    commit: str = ""
    binary_name: str = ""  # main executable of a tool, without .exe
    pip_extra: list[str] = field(default_factory=list)  # extra requirements for a git tool's environment
    requires_java: int = 0  # minimum Java version (Ghidra)

    @property
    def proprietary(self) -> bool:
        return self.license.lower() == "proprietary"

    def parts_for(self, plat: str | None = None) -> list[dict]:
        plat = plat or current_platform()
        return [p for p in self.parts if plat in p.get("platforms", [plat])]

    @property
    def via_linux(self) -> bool:
        """A Linux-only toolchain where only a Linux side can run it: installed from its Linux build and run inside
        WSL 2 on Windows (wsl.py) or in the compiler container on macOS (linuxbox.py)."""
        return (wsl.wanted() or linuxbox.wanted()) and not self.git and not self.parts_for() and bool(self.parts_for(LINUX))

    def linux_parts(self) -> list[dict]:
        return [p for p in self.parts if WSL_PLATFORM in p.get("platforms", [])] or self.parts_for(LINUX)

    @property
    def supported(self) -> bool:
        """Whether this toolchain can be installed on this computer (on Windows, Linux ones through WSL 2)."""
        return bool(self.git) or bool(self.parts_for()) or self.via_linux


def current_platform() -> str:
    """linux-x86_64, linux-aarch64, macos-x86_64, macos-arm64 or windows-x86_64."""
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)
    machine = _platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64" if os_name == "macos" else "aarch64",
               "aarch64": "aarch64" if os_name != "macos" else "arm64"}.get(machine, machine)
    return f"{os_name}-{machine}"


def unsupported_cpu() -> str | None:
    """Why this computer can't run the games' compilers, or None: they're x86 programs, which an ARM computer
    (a Raspberry Pi, an ARM laptop or server) doesn't run."""
    machine = current_platform().split("-", 1)[1]
    if machine == "x86_64":
        return None
    return (f"decomp.wtf needs a 64-bit Intel or AMD PC for now (the games' compilers are x86 programs; this "
            f"computer's processor is {machine})")


@functools.cache
def _registry_data() -> dict:
    return tomllib.loads(resources.files("decompnet").joinpath("toolchains.toml").read_text(encoding="utf-8"))


def registry() -> dict[str, Toolchain]:
    return {tid: Toolchain(id=tid, **spec) for tid, spec in _registry_data().items()}


def get(tid: str) -> Toolchain:
    reg = registry()
    if tid not in reg:
        raise ToolchainError(f"unknown toolchain {tid!r}; known: {', '.join(sorted(reg))}")
    return reg[tid]


def data_dir() -> Path:
    """Per-user data directory for decompnet (toolchains, caches)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "decompnet"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "decompnet"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "decompnet"


def root() -> Path:
    env = os.environ.get("DECOMPNET_TOOLCHAINS")
    return Path(env).expanduser() if env else data_dir() / "toolchains"


def path(tid: str, linux: bool | None = None) -> Path:
    """Where a toolchain is installed. `linux`: its Linux build for WSL or the container (default: when that's
    how it runs here)."""
    if linux is None:
        linux = tid in _registry_data() and get(tid).via_linux
    return root() / ("wsl" if wsl.wanted() else "linux") / tid if linux else root() / tid


def installed(tid: str, linux: bool | None = None) -> dict | None:
    m = path(tid, linux) / MANIFEST
    return json.loads(m.read_text(encoding="utf-8")) if m.exists() else None


def require(tid: str, linux: bool | None = None) -> Path:
    """Directory of an installed toolchain, or a clear error telling the operator how to install it."""
    if installed(tid, linux) is None:
        raise ToolchainError(f"toolchain {tid!r} is not installed: run `decompnet toolchain install {tid}`")
    return path(tid, linux)


def runtime_dirs(tid: str) -> tuple[Path, ...]:
    """The toolchain plus everything it requires, for mounting into the sandbox (all Linux builds on a Linux side)."""
    tc = get(tid)
    linux = tc.via_linux
    return (require(tid, linux), *(require(dep, linux) for dep in tc.requires))


# ---------------------------------------------------------------------------- install

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, log) -> None:
    log(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "decompnet-toolchain-installer"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _deb_data(deb: Path, out: Path) -> Path:
    """The data.tar.* inside a Debian package (an `ar` archive), written to `out`."""
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ToolchainError(f"{deb.name} isn't a Debian package")
        while len(head := f.read(60)) == 60:
            name, size = head[:16].decode().strip().rstrip("/"), int(head[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("data.tar"):
                (out / name).write_bytes(data)
                return out / name
    raise ToolchainError(f"{deb.name} has no data.tar")


def _extract(archive: Path, dest: Path, subdir: str | None) -> None:
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        if archive.name.endswith(".deb"):
            with tempfile.TemporaryDirectory(dir=dest.parent) as raw:
                with tarfile.open(_deb_data(archive, Path(raw))) as t:
                    t.extractall(tmp, filter="data")
        elif archive.name.endswith(".zip"):
            import zipfile

            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    target = (Path(tmp) / info.filename).resolve()
                    if Path(tmp).resolve() not in target.parents and target != Path(tmp).resolve():
                        raise ToolchainError(f"refusing to extract {info.filename} outside the toolchain folder")
                    z.extract(info, tmp)
                    mode = info.external_attr >> 16
                    if mode & 0o111 and not info.is_dir():
                        os.chmod(target, 0o755)
        elif archive.name.endswith(".zst"):
            # GNU tar extracts it; never restore owners from the archive. The zstd layer is undone here first: tar
            # would run the zstd command for it, which many systems don't have (tar then just fails with code 2)
            import zstandard

            plain = Path(tmp).with_name(Path(tmp).name + ".tar")
            try:
                with open(archive, "rb") as src, open(plain, "wb") as out:
                    zstandard.ZstdDecompressor().copy_stream(src, out)
                subprocess.run(["tar", "-xf", str(plain), "-C", tmp, "--no-same-owner", "--no-same-permissions"],
                               check=True, capture_output=True)
            finally:
                plain.unlink(missing_ok=True)
        else:
            # gz/xz in pure Python, so this works on Windows and macOS too. The "data" filter refuses
            # absolute paths, links out of the tree and device files, and drops setuid bits.
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")
        src = Path(tmp) / subdir if subdir else Path(tmp)
        if not src.is_dir():
            raise ToolchainError(f"archive {archive.name} has no {subdir}/")
        for item in src.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
            shutil.move(str(item), target)


def side_name() -> str:
    """Which Linux side runs Linux-only toolchains here: wsl or docker."""
    return "wsl" if wsl.wanted() else "docker"


def _where() -> str:
    return "inside WSL 2" if side_name() == "wsl" else "in the compiler container (Docker)"


def side_status(prepare: bool = False, log=print) -> tuple[bool, str]:
    """Whether the Linux side for Linux-only toolchains works here: (usable, why not). `prepare` builds what's
    missing (the compiler image on a Mac)."""
    if wsl.wanted():
        ok, why = wsl.status()
        return ok, "" if ok else f"on Windows it runs in WSL 2. {why}"
    if prepare:
        try:
            linuxbox.prepare(log)
        except RuntimeError as e:
            return False, f"on a Mac it runs in a Docker container. {e}"
    ok, why = linuxbox.status()
    return ok, "" if ok else f"on a Mac it runs in a Docker container. {why}"


def install(tid: str, *, accept_license: bool = False, force: bool = False, log=print, linux: bool | None = None) -> Path:
    """Install a toolchain (and what it requires). `linux`: its Linux build, to run on a Linux side (default: when
    that's how it runs on this computer)."""
    tc = get(tid)
    linux = tc.via_linux if linux is None else linux
    if linux:
        ok, why = side_status(prepare=True, log=log)
        if not ok:
            raise ToolchainError(f"{tc.name} is a Linux program: {why}")
    for dep in tc.requires:
        if installed(dep, linux) is None or force:
            install(dep, accept_license=accept_license, force=force, log=log, linux=linux)
    if installed(tid, linux) and not force:
        log(f"{tid}: already installed at {path(tid, linux)}")
        return path(tid, linux)
    parts = tc.linux_parts() if linux else tc.parts_for()
    if not parts and not tc.git:
        why = unsupported_cpu()
        raise ToolchainError(f"{tid} ({tc.name}) has no build for this computer ({current_platform()})"
                             + (f": {why}" if why else ""))
    if tc.requires_java and java_version() < tc.requires_java:
        raise ToolchainError(f"{tc.name} needs Java {tc.requires_java} or newer: install it from https://adoptium.net first")
    if tc.proprietary and not accept_license:
        raise ToolchainError(
            f"{tid} ({tc.name}) is proprietary software. Running it on your coordinator is your own licensing "
            f"decision; re-run with --accept-license if you accept that.")

    dest = path(tid, linux)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    log(f"{tid}: installing {tc.name} [{tc.license}]" + (f" (Linux build, runs {_where()})" if linux else ""))
    pinned = []
    try:
        if tc.git:
            _install_git_tool(tc, dest, log)
        downloads = root() / ".downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        for part in parts:
            archive = downloads / Path(part["url"]).name
            if not archive.exists() or (part.get("sha256") and _sha256(archive) != part["sha256"]):
                _download(part["url"], archive, log)
            digest = _sha256(archive)
            if part.get("sha256") and digest != part["sha256"]:
                archive.unlink()
                raise ToolchainError(f"checksum mismatch for {part['url']}: expected {part['sha256']}, got {digest}")
            if not part.get("sha256"):
                log(f"  no pinned checksum in the registry; pinning on first use: sha256 {digest}")
            if part.get("binary"):  # a plain executable, not an archive
                shutil.copyfile(archive, dest / part["binary"])
                os.chmod(dest / part["binary"], 0o755)
            else:
                _extract(archive, dest, part.get("subdir"))
                if part.get("executable"):  # archives that don't record the executable bit
                    for f in dest.rglob("*"):
                        if f.is_file():
                            os.chmod(f, 0o755)
            pinned.append({"url": part["url"], "sha256": digest})
        if linux and wsl.wanted() and "runtime-i386" in tc.requires:  # WSL systems rarely have a 32-bit loader: bring ours
            n = wsl.wrap_i386(dest, wsl.path(path("runtime-i386", linux=True)))
            log(f"  {n} 32-bit programs start through the pinned 32-bit runtime")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    (dest / MANIFEST).write_text(json.dumps({
        "id": tid, "name": tc.name, "license": tc.license, "source": tc.source,
        "parts": pinned, "commit": tc.commit or None, "installed_at": time.time(),
        "license_accepted": bool(tc.proprietary and accept_license), "linux": linux,
    }, indent=2), encoding="utf-8")
    log(f"{tid}: installed at {dest}")
    return dest


def venv_bin(venv: Path, name: str) -> Path:
    """Path of an executable inside a virtual environment, on any OS."""
    return venv / "Scripts" / f"{name}.exe" if sys.platform == "win32" else venv / "bin" / name


def _install_git_tool(tc: Toolchain, dest: Path, log) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise ToolchainError(f"{tc.id} needs `uv` to create its isolated environment")
    if not shutil.which("git"):
        # uv can fetch the pinned commit as an archive when git is missing
        spec = f"{tc.id} @ {tc.git.removesuffix('.git')}/archive/{tc.commit}.zip"
    else:
        spec = f"{tc.id} @ git+{tc.git}@{tc.commit}"
    log(f"  creating an isolated environment for {tc.git} @ {tc.commit[:10]}")
    subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(dest / "venv")], check=True)
    subprocess.run([uv, "pip", "install", "--quiet", "--python", str(venv_bin(dest / "venv", "python")), spec, *tc.pip_extra],
                   check=True)


def binary(tid: str) -> Path:
    """The main executable of an installed tool (objdiff-cli, dtk, coddog, ...), on any OS."""
    tc = get(tid)
    base = require(tid)
    name = tc.binary_name or tid
    for candidate in (base / f"{name}.exe", base / name, base / "bin" / name):
        if candidate.exists():
            return candidate
    raise ToolchainError(f"{tid} is installed but {name} wasn't found in {base}")


def ensure(tid: str, log=print) -> Path:
    """Install a free tool on first use and return its folder. Proprietary ones must be installed explicitly."""
    if installed(tid) is None:
        tc = get(tid)
        if tc.proprietary:
            raise ToolchainError(f"{tc.name} is proprietary: install it from Setup if you accept its license")
        install(tid, log=log)
    return path(tid)


def java_version() -> int:
    """Major version of the java on PATH, 0 if none."""
    import re as _re

    exe = shutil.which("java")
    if not exe:
        return 0
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = _re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(m.group(1)) if m else 0


def status() -> list[dict]:
    rows = []
    for tc in registry().values():
        m = installed(tc.id)
        rows.append({"id": tc.id, "name": tc.name, "kind": tc.kind, "platform": tc.platform, "license": tc.license,
                     "installed": m is not None, "supported": tc.supported, "description": tc.description,
                     "linux": tc.via_linux, "linux_side": side_name() if tc.via_linux else "",
                     "pinned": all(p.get("sha256") for p in tc.parts) or bool(tc.commit)})
    return rows
