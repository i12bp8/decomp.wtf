"""ROM compiler selection: function override > segment range > global range > default.

Ranges are half-open VRAM intervals [start, end), selected by the function's entry
address. A segment is the function's stored unit (the ROM splitter's segment name).
Overlaps are forbidden within one scope; a segment range deliberately overrides a
global range. Profiles replace toolchain and flags, inheriting only execution
settings (timeout, sandbox, include paths, etc.) from the default compiler.
"""

from __future__ import annotations

from . import toolchains

UNSET = object()


def validate(cfg: dict, function_names: set[str] | None = None) -> None:
    profiles = cfg.get("compiler_profiles", {})
    ranges = cfg.get("compiler_ranges", [])
    functions = cfg.get("compiler_functions", {})
    if not isinstance(profiles, dict):
        raise ValueError("compiler_profiles must be an object")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("compiler profile ids must be nonempty strings")
        if not isinstance(profile, dict) or set(profile) != {"toolchain", "flags"}:
            raise ValueError(f"compiler profile {name!r} needs exactly toolchain and flags")
        if not isinstance(profile["toolchain"], str):
            raise ValueError(f"compiler profile {name!r} toolchain must be a registered id")
        toolchains.get(profile["toolchain"])
        validate_flags(profile["flags"])
    if not isinstance(ranges, list):
        raise ValueError("compiler_ranges must be an array")
    scopes: dict[str | None, list[dict]] = {}
    segments = {s["name"] for s in cfg.get("segments", []) if isinstance(s, dict) and "name" in s}
    for item in ranges:
        if not isinstance(item, dict) or not {"start", "end", "profile"} <= item.keys() or item.keys() - {"start", "end", "profile", "segment"}:
            raise ValueError("compiler ranges need start, end, profile and optionally segment")
        start, end = item["start"], item["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 1 << 32:
            raise ValueError("compiler ranges need integer VRAM bounds 0 <= start < end <= 2^32")
        _profile_exists(item["profile"], profiles)
        segment = item.get("segment")
        if "segment" in item and (not isinstance(segment, str) or not segment):
            raise ValueError("compiler range segment must be a nonempty segment name")
        if segment is not None and segments and segment not in segments:
            raise ValueError(f"unknown compiler range segment {segment!r}")
        scopes.setdefault(segment, []).append(item)
    for segment, items in scopes.items():
        ordered = sorted(items, key=lambda item: item["start"])
        if any(a["end"] > b["start"] for a, b in zip(ordered, ordered[1:])):
            raise ValueError(f"overlapping compiler ranges in scope {segment or '<global>'!r}")
    if not isinstance(functions, dict):
        raise ValueError("compiler_functions must be an object")
    for name, profile in functions.items():
        if not isinstance(name, str) or not name:
            raise ValueError("compiler function names must be nonempty strings")
        if function_names is not None and name not in function_names:
            raise ValueError(f"unknown compiler function {name!r}")
        _profile_exists(profile, profiles)


def validate_flags(flags) -> None:
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise ValueError("compiler flags must be an array of strings")


def _profile_exists(profile, profiles: dict) -> None:
    if not isinstance(profile, str) or profile not in profiles:
        raise ValueError(f"unknown compiler profile {profile!r}")


def effective_compiler(cfg: dict, fn=None) -> dict:
    compiler = dict(cfg["compiler"])
    if fn is None:
        return compiler
    profile = cfg.get("compiler_functions", {}).get(fn["name"])
    if profile is None and fn["vram"] is not None:
        global_profile = None
        for item in cfg.get("compiler_ranges", []):
            if item["start"] <= fn["vram"] < item["end"]:
                if "segment" not in item:
                    global_profile = item["profile"]
                elif item["segment"] == fn["unit"]:
                    profile = item["profile"]
                    break
        if profile is None:
            profile = global_profile
    if profile is not None:
        # A default command template must never shadow the selected registry compiler.
        compiler.pop("cmd", None)
        compiler.update(cfg["compiler_profiles"][profile], profile=profile)
    return compiler
