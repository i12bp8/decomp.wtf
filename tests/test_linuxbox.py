"""Linux-only compilers on a Mac, through the compiler container (linuxbox.py).

A stand-in for the docker command keeps the container's state in a folder and runs `exec`/`run` commands on this
computer, so what's checked is what decomp.wtf asks Docker for: a long-lived container reused across compiles and
recreated when its mounts change, the compile folder inside the shared area, and a timeout the way coreutils reports it.
"""

import sys

import pytest

from decompnet import compiler, linuxbox, toolchains

FAKE = r"""#!/bin/sh
state="$FAKE_DOCKER_STATE"; echo "$@" >> "$state/log"
case "$1" in
  info) echo 27.0 ;;
  image) exit 0 ;;
  rm) rm -f "$state/label"; exit 0 ;;
  inspect) [ -f "$state/label" ] || exit 1; echo "true $(cat "$state/label")" ;;
  run) case "$2" in
         -d) while [ $# -gt 0 ]; do case "$1" in --label) echo "${2#decomp.mounts=}" > "$state/label" ;; esac; shift; done ;;
         *) echo "unexpected one-shot: $*" >&2; exit 9 ;;
       esac ;;
  exec) shift 6  # exec -i NAME timeout -k 2 <N> sh -s: coreutils' timeout isn't on a Mac, so perl's alarm stands in
        perl -e 'alarm shift; exec @ARGV' "$1" sh -s; rc=$?; [ $rc -eq 142 ] && exit 124; exit $rc ;;
esac
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    fake = tmp_path / "docker"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("DECOMP_LINUXBOX", str(fake))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    monkeypatch.setattr(toolchains, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(linuxbox, "_status", None)
    monkeypatch.setattr(linuxbox, "_ready", None)
    return tmp_path


def starts(box) -> int:
    return sum(1 for line in (box / "state" / "log").read_text().splitlines() if line.startswith("run -d"))


def test_one_container_serves_every_compile_and_follows_its_mounts(box, monkeypatch):
    cfg = compiler.CompilerConfig(cmd=["sh", "-c", "echo 'int x;' > func.o"], linux=True)
    for _ in range(3):
        res = compiler.compile_source(cfg, "", "int f(void) { return 1; }")
        assert res.ok, res.log
        assert res.obj == b"int x;\n"
    assert starts(box) == 1

    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(box / "elsewhere"))  # the toolchain folder moved: new mounts
    linuxbox._ready = None
    assert compiler.compile_source(cfg, "", "int f(void) { return 1; }").ok
    assert starts(box) == 2


def test_compiles_are_made_where_the_container_can_see_them(box, monkeypatch):
    seen = []
    real = linuxbox.run

    def spy(argv, cwd, *a, **kw):
        seen.append(cwd)
        return real(argv, cwd, *a, **kw)

    cfg = compiler.CompilerConfig(cmd=["sh", "-c", "echo x > func.o"], linux=True)
    monkeypatch.setattr(linuxbox, "run", spy)
    compiler.compile_source(cfg, "", "int f(void) { return 1; }")
    assert seen and all(p.parent == linuxbox.work_root() for p in seen)


def test_a_compile_that_overruns_is_reported_as_a_timeout(box):
    cfg = compiler.CompilerConfig(cmd=["sleep", "4"], linux=True, timeout=1)
    res = compiler.compile_source(cfg, "", "int f(void) { return 1; }")
    assert not res.ok and "timed out" in res.log


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_without_docker_the_app_says_how_to_get_it(box, monkeypatch):
    monkeypatch.setenv("DECOMP_LINUXBOX", str(box / "missing-docker"))
    monkeypatch.setattr(linuxbox, "_status", None)
    ok, why = toolchains.side_status()
    assert not ok and "Docker" in why
    with pytest.raises(toolchains.ToolchainError, match="Docker"):
        toolchains.install("ee-gcc-2.96", log=lambda *_: None)
