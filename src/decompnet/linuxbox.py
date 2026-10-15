"""Running the Linux-only period compilers on macOS, through a compiler-only Docker container.

KMC GCC (N64), the PlayStation GCCs, EE-GCC (PS2) and agbcc (GBA) exist only as x86 Linux programs. On a Mac they're
installed like any toolchain (downloaded and checked on the Mac side, under the user's toolchain folder) and run in
one long-lived container made from linuxbox.Dockerfile: bare Debian with the 32-bit C library, no decomp.wtf in it.
The toolchain folder is mounted read-only and the compile folders read-write, both at the same path as on the Mac,
so a command line means the same thing on either side. The container has no network and is resource-limited.

Same shape as wsl.py: `wanted`, `status`, `run`. The shell script that stages and runs a compile is wsl.script.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from importlib import resources
from pathlib import Path

from . import wsl

IMAGE = "decomp-wtf-compiler"
NAME = "decomp-wtf-compiler"
INSTALL = "Install and start Docker Desktop (https://docs.docker.com/desktop/setup/install/mac-install/)"
_lock = threading.Lock()
_status: tuple[bool, str, float] | None = None
_ready: str | None = None  # the mounts of the container known to run


def wanted() -> bool:
    """Whether Linux-only toolchains go through the container here (macOS; DECOMP_LINUXBOX points tests at a
    stand-in for the docker command)."""
    return sys.platform == "darwin" or bool(os.environ.get("DECOMP_LINUXBOX"))


def exe() -> str | None:
    return shutil.which(os.environ.get("DECOMP_LINUXBOX") or "docker")


def _docker(args: list[str], timeout: float, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([exe() or "docker", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, input=stdin, stdin=None if stdin else subprocess.DEVNULL)


def work_root() -> Path:
    """Where compiles are made so the container can see them (a folder Docker Desktop shares by default)."""
    from . import toolchains
    return toolchains.data_dir() / "box-work"


def _mounts() -> list[tuple[str, str]]:
    """(folder, mode): what the long-lived container sees."""
    from . import toolchains
    work_root().mkdir(parents=True, exist_ok=True)
    toolchains.root().mkdir(parents=True, exist_ok=True)
    return [(str(toolchains.root().resolve()), "ro"), (str(work_root().resolve()), "rw")]


def status() -> tuple[bool, str]:
    """(usable, why not): Docker answers and the compiler image exists. A yes is kept; a no is checked again after
    a minute (the user may be starting Docker right now)."""
    global _status
    with _lock:
        if _status is not None and (_status[0] or time.time() - _status[2] < 60):
            return _status[0], _status[1]
        _status = (*_check(), time.time())
        return _status[0], _status[1]


def _check() -> tuple[bool, str]:
    if not exe():
        return False, f"Docker isn't installed. {INSTALL}."
    try:
        info = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=30)
        if info.returncode != 0:
            return False, f"Docker isn't running ({(info.stderr or info.stdout).strip()[-200:]}). {INSTALL}."
        if _docker(["image", "inspect", IMAGE], timeout=30).returncode != 0:
            return False, (f"the compiler image {IMAGE!r} isn't built yet: run "
                           "`decompnet toolchain install <toolchain>` (it builds the image first).")
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Docker didn't answer ({e}). {INSTALL}."
    return True, ""


def prepare(log=print) -> None:
    """Build the compiler image if it isn't there (one minute, once)."""
    global _status
    if not exe():
        raise RuntimeError(f"Docker isn't installed. {INSTALL}.")
    if _docker(["image", "inspect", IMAGE], timeout=30).returncode == 0:
        return
    log(f"building the compiler image {IMAGE!r} (once)")
    dockerfile = resources.files("decompnet").joinpath("linuxbox.Dockerfile").read_text(encoding="utf-8")
    res = _docker(["build", "--platform", "linux/amd64", "-t", IMAGE, "-"], timeout=900, stdin=dockerfile)
    if res.returncode != 0:
        raise RuntimeError(f"building {IMAGE!r} failed:\n{(res.stdout + res.stderr)[-1500:]}")
    with _lock:
        _status = None


def _flags(mounts: list[tuple[str, str]]) -> list[str]:
    out = ["--platform", "linux/amd64", "--network", "none", "--memory", "2g", "--pids-limit", "1024"]
    for p, mode in mounts:
        out += ["-v", f"{p}:{p}:{mode}"]
    return out


def _ensure() -> None:
    """The long-lived container, running with the current mounts (recreated when the toolchain folder moved)."""
    global _ready
    mounts = _mounts()
    want = hashlib.sha256(repr(mounts).encode()).hexdigest()[:16]
    with _lock:
        if _ready == want:
            return
        state = _docker(["inspect", "-f", '{{.State.Running}} {{index .Config.Labels "decomp.mounts"}}', NAME], 30)
        if state.returncode == 0 and state.stdout.split() == ["true", want]:
            _ready = want
            return
        _docker(["rm", "-f", NAME], 60)
        res = _docker(["run", "-d", "--name", NAME, "--init", "--restart", "unless-stopped",
                       "--label", f"decomp.mounts={want}", *_flags(mounts), IMAGE], 120)
        if res.returncode != 0:
            raise RuntimeError(f"couldn't start the compiler container:\n{(res.stdout + res.stderr)[-1500:]}")
        _ready = want


def _inside(p: Path, folder: Path) -> bool:
    return p.resolve().is_relative_to(folder.resolve())


def run(argv: list[str], cwd: Path, timeout: float, *, outputs: list[str] | None = None,
        inputs: list[str] | None = None, stage: str | None = None) -> subprocess.CompletedProcess:
    """Run `argv` in the container in `cwd` (the same path there). A folder outside the compile area (an exported
    project being built) gets a throwaway container of its own with it mounted. With `outputs` the compile runs on a
    copy in the container's own file system, see wsl.script."""
    body = wsl.script(str(cwd), argv, outputs=outputs, inputs=inputs, stage=stage)
    guard = ["timeout", "-k", "2", str(max(1, int(timeout))), "sh", "-s"]
    if _inside(cwd, work_root()):
        _ensure()
        cmd = ["exec", "-i", NAME, *guard]
    else:
        cmd = ["run", "--rm", "-i", *_flags([*_mounts()[:1], (str(cwd.resolve()), "rw")]), "--entrypoint", guard[0],
               IMAGE, *guard[1:]]
    res = subprocess.run([exe() or "docker", *cmd], input=body, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout + 15)
    if res.returncode == 124:  # coreutils timeout
        raise subprocess.TimeoutExpired(res.args, timeout)
    return subprocess.CompletedProcess(res.args, res.returncode, res.stdout, res.stderr)
