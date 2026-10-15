"""DecompNet: collaborative matching decompilation for AI agents."""

import sys

__version__ = "0.10.4"


def utf8_output() -> None:
    """Windows: output into a pipe or file is written in the ANSI code page there, where printing a "→" or a user name
    with letters outside it crashes. Write UTF-8 instead (a console window shows everything anyway)."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")


if sys.platform in ("win32", "darwin"):
    # Python only trusts the root certificates Windows has already cached, and Windows fetches the others the first
    # time a browser needs them: downloads from some hosts (developer.arm.com) fail with CERTIFICATE_VERIFY_FAILED.
    # The Python builds uv installs on a Mac don't use the system's certificates at all, with the same failure.
    # Verify through the system itself instead, like every other program there does.
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
