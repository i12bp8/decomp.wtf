"""Which compiler judges a function: function override > segment range > global range > project default."""

import pytest

from decompnet.compiler_profiles import effective_compiler, validate

CFG = {
    "compiler": {"toolchain": "ee-gcc-2.96", "flags": ["-O2"], "timeout": 20, "cmd": ["stale"]},
    "segments": [{"name": "game"}, {"name": "lib"}],
    "compiler_profiles": {"mwcc": {"toolchain": "gcc-2.7.2-psx", "flags": ["-O1"]},
                          "other": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O3"]}},
    "compiler_ranges": [{"start": 0x100000, "end": 0x200000, "profile": "mwcc"},
                        {"start": 0x150000, "end": 0x160000, "profile": "other", "segment": "lib"}],
    "compiler_functions": {"pinned": "other"},
}


def fn(name, vram, unit="game"):
    return {"name": name, "vram": vram, "unit": unit}


def test_the_most_specific_assignment_wins():
    pick = lambda f: effective_compiler(CFG, f).get("profile")
    assert pick(fn("a", 0x0FFFFF)) is None                       # below every range: the default
    assert pick(fn("a", 0x100000)) == "mwcc"                     # range starts are inclusive
    assert pick(fn("a", 0x200000)) is None                       # range ends are exclusive
    assert pick(fn("a", 0x155000, "game")) == "mwcc"             # a segment range only covers its own segment
    assert pick(fn("a", 0x155000, "lib")) == "other"             # ...and beats the global one there
    assert pick(fn("pinned", 0x100000)) == "other"               # a function override beats every range
    assert pick(fn("a", None)) is None


def test_a_profile_replaces_the_compiler_but_keeps_how_it_runs():
    assert effective_compiler(CFG) == CFG["compiler"]
    mwcc = effective_compiler(CFG, fn("a", 0x100000))
    assert (mwcc["toolchain"], mwcc["flags"], mwcc["timeout"]) == ("gcc-2.7.2-psx", ["-O1"], 20)
    assert "cmd" not in mwcc  # the default's command template must not shadow the profile's toolchain
    assert CFG["compiler"]["flags"] == ["-O2"] and CFG["compiler"]["cmd"] == ["stale"]  # the config is untouched


@pytest.mark.parametrize("change, why", [
    ({"compiler_ranges": [{"start": 0, "end": 10, "profile": "mwcc"}, {"start": 5, "end": 20, "profile": "other"}]},
     "overlapping"),
    ({"compiler_ranges": [{"start": 10, "end": 10, "profile": "mwcc"}]}, "VRAM bounds"),
    ({"compiler_ranges": [{"start": 0, "end": 10, "profile": "nope"}]}, "unknown compiler profile"),
    ({"compiler_ranges": [{"start": 0, "end": 10, "profile": "mwcc", "segment": "gone"}]}, "unknown compiler range segment"),
    ({"compiler_functions": {"missing": "mwcc"}}, "unknown compiler function"),
    ({"compiler_profiles": {"x": {"toolchain": "no-such-compiler", "flags": []}}}, "unknown toolchain"),
])
def test_bad_assignments_are_refused(change, why):
    with pytest.raises(Exception, match=why):
        validate({**CFG, "compiler_ranges": [], "compiler_functions": {}, **change}, {"pinned", "a"})


def test_segment_ranges_may_overlap_global_ones():
    validate(CFG, {"pinned"})
