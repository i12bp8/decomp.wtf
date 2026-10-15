"""The local engine: ROM projects on top of the coordination core.

Network (claims, attempts, best attempts, notes, a regression-tested shared header) already does the
bookkeeping several agents need to work on one codebase. A ROM project adds:

- targets taken straight from the ROM (splat), matched by linking the candidate at the function's
  address (rom.match), so a match is byte-identical code;
- a symbol table, so agents can name things (rename_symbol) without breaking matches;
- tools for what ROMs need: split a function the disassembler merged, read data, see who calls what.

Everything stays on this computer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import struct
import threading
import time
from pathlib import Path

from .. import scoring, seeds, similarity, toolchains
from ..elfobj import FunctionObj
from ..network import Agent, Network, NetworkError, _hash_token
from ..rom import detect, match, split
from ..rom.symbols import AUTO_NAME, IDENT, Symbol, SymbolTable

DEFAULT_TOOLCHAIN = {"KMC": "gcc-2.7.2-kmc", "IDO": "ido-7.1", "PSYQ": "gcc-2.7.2-psx", "EEGCC": "ee-gcc-2.96",
                     "AGBCC": "agbcc"}
# How ready each platform is for a full decomp you can port and mod; the GUI and the README show it.
# recommended: the most tested path, from import to a project that builds the game (GameCube and Wii: the same kind
# of project as decomp.dev's, linked file by file as it's finished; N64: tested on real games, rebuilt byte for byte).
# beta: works and is tested on homemade games, less so on real ones yet.
PLATFORM_STATUS = {"gc": "recommended", "wii": "recommended", "n64": "recommended",
                   "psx": "beta", "ps2": "beta", "gba": "beta", "nds": "beta"}
COMPILERS = {
    "gcc-2.7.2-kmc": "many third-party N64 games",
    "ido-7.1": "most later first-party N64 games",
    "ido-5.3": "early N64 games (1996-97)",
    "gcc-2.7.2-psx": "PlayStation games (PSY-Q era GCC)",
    "gcc-2.8.1-psx": "later PlayStation games",
    "ee-gcc-2.96": "PlayStation 2 games (Sony's EE GCC)",
    "agbcc": "Game Boy Advance games (pret's agbcc, GCC 2.95)",
}
SHORT_NAMES = {"gcc-2.7.2-kmc": "KMC GCC 2.7.2", "ido-7.1": "IDO 7.1", "ido-5.3": "IDO 5.3",
               "gcc-2.7.2-psx": "GCC 2.7.2 (PS1)", "gcc-2.8.1-psx": "GCC 2.8.1 (PS1)", "ee-gcc-2.96": "EE-GCC 2.96",
               "agbcc": "agbcc"}
# CodeWarrior versions in the `mwcc` pack (GameCube, Wii) and the `mwccarm` pack (DS), for games started from scratch.
MW_VERSIONS = {
    "gc": ["GC/1.0", "GC/1.1", "GC/1.1p1", "GC/1.2.5", "GC/1.2.5n", "GC/1.3", "GC/1.3.2", "GC/1.3.2r", "GC/2.0",
           "GC/2.0p1", "GC/2.5", "GC/2.6", "GC/2.7", "GC/3.0a3", "GC/3.0a3.2", "GC/3.0a3.3", "GC/3.0a3.4", "GC/3.0a5",
           "GC/3.0a5.2"],
    "wii": ["Wii/0x4201_127", "Wii/1.0RC1", "Wii/1.0a", "Wii/1.0", "Wii/1.1", "Wii/1.3", "Wii/1.5", "Wii/1.6", "Wii/1.7"],
    "ds": ["DS/1.2/base", "DS/1.2/sp2", "DS/1.2/sp2p3", "DS/1.2/sp3", "DS/1.2/sp4", "DS/2.0/base", "DS/2.0/sp1",
           "DS/2.0/sp1p2", "DS/2.0/sp1p5", "DS/2.0/sp1p6", "DS/2.0/sp1p7", "DS/2.0/sp2", "DS/2.0/sp2p2", "DS/2.0/sp2p3",
           "DS/2.0/sp2p4", "DS/dsi/1.1", "DS/dsi/1.1p1", "DS/dsi/1.2", "DS/dsi/1.2p1", "DS/dsi/1.2p2", "DS/dsi/1.3",
           "DS/dsi/1.3p1", "DS/dsi/1.6sp1", "DS/dsi/1.6sp2"],
}

# Tools shown in Setup besides the compilers.
TOOLS = {
    "m2c": "machine-made first drafts (MIPS, PowerPC, ARM)",
    "objdiff-cli": "judges forks of public decomps, function by function",
    "ghidra": "drafts for PC, Switch and other platforms m2c can't read (540 MB, needs Java 21+)",
    "mwcc": "CodeWarrior, the compilers of GameCube and Wii games (proprietary)",
    "mwccarm": "CodeWarrior for DS, the compilers of DS games (proprietary)",
    "dsd": "ds-decomp: analyses DS ROMs and rebuilds them",
}
# Functions this small are handed out in batches (claim_function count): agents match most on the first try.
BATCH_INSNS = 48
PLATFORM_COMPILERS = {"n64": ["gcc-2.7.2-kmc", "ido-7.1", "ido-5.3"], "psx": ["gcc-2.7.2-psx", "gcc-2.8.1-psx"],
                      "ps2": ["ee-gcc-2.96"], "gba": ["agbcc"]}
_AUTO_TOKEN = re.compile(r"\b(?:func|D|B|jtbl|RO|STR|FLT|DBL)_[0-9A-F]{8}\b")
_ADDRESS = re.compile(r"^(?:0x)?([0-9A-Fa-f]{8})$")

GP_NOTE = """
/* This game keeps small globals (8 bytes or less) in $gp-relative small data. Declare those here WITHOUT
 * `extern` (for example `s32 D_8001B278;`): then the compiler reads them through $gp, like the game does.
 * With `extern`, it builds the full address instead and the function can't match. */
"""

BASE_HEADER = """/* context.h for {title}: shared by every function. Agents grow it with edit_context:
 * add a prototype or struct the moment you work one out, so the next function benefits. */

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef volatile u8 vu8;
typedef volatile u16 vu16;
typedef volatile u32 vu32;
typedef volatile s32 vs32;
typedef int s32_bool;

#define NULL ((void *)0)
#define TRUE 1
#define FALSE 0
#define ARRAY_COUNT(arr) (s32)(sizeof(arr) / sizeof(arr[0]))
"""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "rom"


def _preview(data: bytes | None, kind: str) -> str:
    if not data:
        return ""
    text = data.split(b"\0", 1)[0]
    if len(text) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in text):
        return json.dumps(text.decode("ascii"))
    return data[:16].hex(" ")


class RomNetwork(Network):
    def __init__(self, db_path: str | Path, workspace: Path, claim_ttl: int = 30 * 60):
        super().__init__(db_path, claim_ttl=claim_ttl)
        self.workspace = Path(workspace)
        self._syms: dict[int, SymbolTable] = {}
        self._sym_override: dict[int, SymbolTable] = {}
        self._roms: dict[int, tuple[bytes, list[split.Segment]]] = {}
        self._xrefs: dict[int, dict] = {}
        self._seed_lock = threading.Lock()
        self.links: dict = {}  # slug -> collab.Link, for projects joined from a friend's room
        self.rooms: dict = {}  # slug -> collab.Room, for projects this computer shares
        self._remote_ids: dict[str, int] = {}
        self._ops: dict[int, dict] = {}  # project id -> look-alike index
        self._diffs: dict[tuple, dict] = {}  # (function, source, context, compiler) -> diff for the GUI
        self.autopilots: dict[str, dict] = {}  # slug -> progress of trying every function's machine draft
        self.autopilot_after_match = False
        self._autopilot_restart_lock = threading.Lock()
        from .forkengine import ForkEngine  # forks build on this class's claims, attempts and live view
        from .permute import Permuter
        from .twins import Twins
        self.forks = ForkEngine(self)
        self.twins = Twins(self)
        self.permuter = Permuter(self)
        self._rescanning: set[int] = set()
        self._active_attempts: dict[int, int] = {}
        self._rescan_previews: dict[str, tuple[str, list, dict]] = {}
        self.rescan_busy = lambda slug: False  # server supplies its built-in agent/session guard

    # ------------------------------------------------------------------ basics

    def _require_budget(self, agent: Agent) -> None:
        """Your machine, your agent: no compile budget."""

    @staticmethod
    def cfg(proj) -> dict:
        return json.loads(proj["config"])

    def is_rom(self, proj) -> bool:
        return self.cfg(proj).get("kind") == "rom"

    def project_row(self, slug: str):
        return self._project(slug)

    def fork_slug(self, project: str | None) -> str | None:
        """The slug when `project` is a fork of a public decomp, else None."""
        try:
            proj = self._project(project)
        except NetworkError:
            return None
        return proj["slug"] if self.cfg(proj).get("kind") == "fork" else None

    def _rom_only(self, project: str | None, what: str) -> None:
        if self.fork_slug(project):
            raise NetworkError(f"{what} is for ROM projects. In a fork, the project's own headers and source files are "
                               "the shared context: use read_file, search_code and edit_file")

    def symbols(self, project_id: int) -> SymbolTable:
        if project_id in self._sym_override:
            return self._sym_override[project_id]
        if project_id not in self._syms:
            rows = self.store.all("SELECT name, address, size, kind FROM symbols WHERE project_id=?", project_id)
            self._syms[project_id] = SymbolTable([Symbol(r["name"], r["address"], r["size"], r["kind"]) for r in rows])
        return self._syms[project_id]

    def _rom(self, proj) -> tuple[bytes, list[split.Segment]]:
        if proj["id"] not in self._roms:
            cfg = self.cfg(proj)
            rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
            segs = [split.Segment(**s) for s in cfg["segments"]]
            self._roms[proj["id"]] = (rom, segs)
        return self._roms[proj["id"]]

    def reader(self, proj):
        rom, segs = self._rom(proj)

        def read(addr: int, n: int) -> bytes | None:
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom[at:at + n]
            return None
        return read

    def _function(self, name: str, project: str | None):
        """Functions can also be named by address (0x80001234), handy for agents reading assembly, and the way
        agents write names they read elsewhere (see _loose_functions)."""
        m = _ADDRESS.match(name.strip()) if name else None
        if m:
            proj = self._project(project)
            row = self.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], int(m.group(1), 16))
            if row is not None:
                return row
        try:
            return super()._function(name, project)
        except NetworkError as e:
            rows = self._loose_functions(name, project) if name else []
            if len(rows) == 1:
                return rows[0]
            if rows:
                raise NetworkError(self._ambiguous(name, rows)) from None
            near = self._near_names(name, project) if name else []
            if near:
                raise NetworkError(f"{e}. Did you mean: {', '.join(near)}?") from None
            raise

    def _near_names(self, name: str, project: str | None, n: int = 5) -> list[str]:
        """Function names close to one that doesn't exist (a typo, another spelling of the symbol)."""
        import difflib

        try:
            proj = self._project(project)
        except NetworkError:
            return []
        sym = name.strip().strip("`'\"").split("@", 1)[0].split("(", 1)[0].strip()
        if len(sym) < 3:
            return []
        rows = self.store.all("SELECT name FROM functions WHERE project_id=? AND (name LIKE ? OR name LIKE ?) LIMIT 4000",
                              proj["id"], f"%{sym[-6:]}%", f"{sym[:4]}%")
        names = [r["name"] for r in rows]
        return difflib.get_close_matches(sym, names, n=n, cutoff=0.6) or [x for x in names if sym in x][:n]

    def _loose_functions(self, name: str, project: str | None) -> list:
        """Functions an agent may mean by a name that isn't stored as such: the plain symbol of a function whose
        name repeats across a fork's files (stored as symbol@unit), symbol@unit for one that doesn't repeat,
        Class::method, a name with its parameter list or in quotes."""
        try:
            proj = self._project(project)
        except NetworkError:
            return []
        text = name.strip().strip("`'\"").strip()
        sym, _, unit = text.partition("@")
        unit, _, nth = unit.partition("#")
        sym = sym.split("(", 1)[0].strip()
        if not sym:
            return []
        rows = self.store.all(
            "SELECT * FROM functions WHERE project_id=? AND (name=? OR substr(name,1,?)=? "
            "OR json_extract(meta,'$.symbol')=? OR substr(json_extract(meta,'$.demangled'),1,?)=?) ORDER BY name",
            proj["id"], sym, len(sym) + 1, sym + "@", sym, len(sym) + 1, sym + "(")
        if unit:
            rows = ([r for r in rows if r["unit"] == unit] or [r for r in rows if r["unit"].endswith(unit)]
                    or [r for r in rows if r["unit"].startswith(unit)] or [r for r in rows if unit in r["unit"]])
        if nth.isdigit() and len(rows) > 1:
            rows = [r for r in rows if (json.loads(r["meta"] or "{}").get("nth", 0) + 1) == int(nth)] or rows
        return [r for r in rows if r["status"] != "nosource"] or rows  # ones in files without source can't be worked on

    @staticmethod
    def _ambiguous(name: str, rows: list) -> str:
        listing = "\n".join(f"- {r['name']} ({r['status']}, {r['n_insns']} instructions)" for r in rows[:12])
        more = f"\n... and {len(rows) - 12} more" if len(rows) > 12 else ""
        return (f"{name!r} is the name of {len(rows)} functions in different files; pass the full name:\n"
                f"{listing}{more}")

    def resolve_function(self, agent: Agent | None, project: str | None, name: str) -> str:
        """The stored name for what an agent calls a function: when a plain name fits several, the one the agent
        holds a claim on."""
        try:
            return self._function(name, project)["name"]
        except NetworkError:
            rows = self._loose_functions(name, project) if name else []
            held = self.store.all("SELECT f.id, f.unit FROM claims c JOIN functions f ON f.id=c.function_id "
                                  "WHERE c.agent_id=?", agent.id) if agent else []
            if held and len(rows) > 1:  # one it holds, else the one in the file (or module) it works in
                ids, units = {h["id"] for h in held}, {h["unit"] for h in held}
                modules = {u.split("/", 1)[0] for u in units}
                for pick in ([r for r in rows if r["id"] in ids], [r for r in rows if r["unit"] in units],
                             [r for r in rows if r["unit"].split("/", 1)[0] in modules]):
                    if len(pick) == 1:
                        return pick[0]["name"]
            raise

    def _rom_target(self, fn) -> match.RomTarget:
        proj = self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])
        cfg = json.loads(proj["config"])
        return match.RomTarget(fn["name"], fn["vram"], fn["target"], match.RomTarget.relocs_from_json(json.loads(fn["relocs"])),
                               cfg.get("big_endian", True), cfg.get("gp", 0))

    def _make_target(self, proj, rf: split.RomFunction, syms: SymbolTable) -> match.RomTarget:
        cfg = self.cfg(proj)
        return match.target_from_split(rf, syms.resolve, cfg.get("big_endian", True), cfg.get("gp", 0))

    def _target_obj(self, fn, proj):
        if not self.is_rom(proj):
            return super()._target_obj(fn, proj)
        if self.cfg(proj).get("platform") == "gba":
            return FunctionObj(fn["name"], "thumb", False, fn["target"], [])
        return match.canonical_target(self._rom_target(fn), self.symbols(proj["id"]))

    def _evaluate(self, fn, proj, obj: bytes):
        if not self.is_rom(proj):
            return super()._evaluate(fn, proj, obj)
        if self.cfg(proj).get("platform") == "gba":
            from ..rom import match_arm

            res = match_arm.link(obj, fn["name"], match_arm.ArmTarget(fn["name"], fn["vram"], fn["target"]),
                                 self.symbols(proj["id"]), self.reader(proj))
            return res.candidate, res.exact if res.candidate is not None else None, res.problems
        res = match.link(obj, fn["name"], self._rom_target(fn), self.symbols(proj["id"]), self.reader(proj))
        return res.candidate, res.exact if res.candidate is not None else None, res.problems

    def _twin_lines(self, proj, fn) -> list[str]:
        """For a brief: the matched function with the same code, and its C with this one's names."""
        try:
            twin_id = self.twins.twin_of(proj, fn["id"])
        except Exception:
            return []
        if twin_id is None:
            return []
        a = self.store.one("SELECT * FROM functions WHERE id=?", twin_id)
        source = self.twins.source_for(proj, a, fn)
        out = ["", f"### Identical code: {a['name']} is the same machine code and already matches"]
        if source and len(source) < 6000:
            out += ["Its C with this function's names (try it first):", "```c", source.strip(), "```"]
        return out

    def rename_tokens(self, proj, text: str) -> str:
        """Show automatic names under their current names (func_80001234 -> deflate)."""
        syms = self.symbols(proj["id"])
        return _AUTO_TOKEN.sub(lambda m: syms.label(int(m.group(0)[-8:], 16)), text)

    def _asm_text(self, fn, proj) -> str:
        if not self.is_rom(proj):
            return super()._asm_text(fn, proj)
        return self.rename_tokens(proj, fn["asm"])

    # ------------------------------------------------------------------ import

    def import_rom(self, data: bytes | Path, *, toolchain: str | None = None, flags: list[str] | None = None,
                   confirmed: bool = False, name: str = "", log=print) -> str:
        """Import a game the user owns (ROM bytes, or a file such as a disc image): split it, store every function
        as a target. Returns the slug."""
        if not confirmed:
            raise NetworkError("confirm that this is your own legally obtained copy of the game")
        project_file = self._community_layout(data, name)
        if project_file is not None:  # a dtk-template or ds-decomp project, worked on like a fork
            compiler = toolchain if toolchain and toolchain.startswith(("GC/", "Wii/", "DS/")) else None
            return self.forks.create_scratch(project_file, compiler=compiler, log=log)
        picked = detect.identify(data, name)
        rom, info = picked.data, picked.info
        for p in self.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("rom", {}).get("sha1") == info["sha1"]:
                log(f"already imported as {p['slug']}")
                return p["slug"]
        base = _slugify(info["title"])
        slug, n = base, 2
        while self.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        workdir = self.workspace / slug
        workdir.mkdir(parents=True)
        (workdir / picked.filename).write_bytes(rom)
        size = f"{info['size'] // (1024 * 1024)} MiB" if info["size"] >= 1 << 20 else f"{info['size'] // 1024} KiB"
        log(f"{detect.PLATFORMS[picked.platform]}: {info['title']} ({info['game_code'] or 'no game code'}, "
            f"{info['region']}), {size}, looks built with {info['compiler_guess']}")
        if picked.platform == "gba":
            return self._import_gba(picked, slug, workdir, toolchain, flags, log)
        if picked.overlays:
            log(f"{len(picked.overlays)} overlay{'s' if len(picked.overlays) != 1 else ''} on the disc: "
                + ", ".join(f"{o[0]} at 0x{o[2]:08X}" for o in picked.overlays[:6]) + (", ..." if len(picked.overlays) > 6 else ""))
        res = split.split_rom(workdir, picked.filename, log=log, overlays=picked.overlays)
        if res.image and res.image != picked.filename:  # splat works on a flat image made from the program
            rom = (workdir / res.image).read_bytes()

        toolchain = toolchain or DEFAULT_TOOLCHAIN.get(res.compiler, DEFAULT_TOOLCHAIN[info["compiler_guess"]])
        tc = toolchains.get(toolchain)
        default_flags = tc.flags
        if picked.platform in ("psx", "ps2"):
            default_flags = [f for f in tc.flags if not f.startswith("-G")] + [f"-G{8 if res.gp else 0}"]
        cfg = {
            "kind": "rom", "platform": picked.platform, "big_endian": picked.big_endian, "gp": res.gp,
            "rom": {**info, "file": res.image or picked.filename, "program": picked.filename},
            "segments": [vars(s) for s in res.segments],
            "compiler": {"toolchain": toolchain, "flags": flags or default_flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        symbols = SymbolTable()
        for name, addr in res.named.items():
            symbols.add(Symbol(name, addr, 0, "func"), reindex=False)
        for fn in res.functions:
            symbols.add(Symbol(fn.name, fn.vram, fn.size, "func"), reindex=False)
        rom_bytes, segs = rom, res.segments

        def read(addr, n):
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom_bytes[at:at + n]
            return None

        data_rows = []
        for d in res.data:
            if d.name in symbols.by_name:
                continue
            symbols.add(Symbol(d.name, d.vram, d.size, d.kind), reindex=False)
            data_rows.append((d.name, d.vram, d.size, d.kind, _preview(read(d.vram, min(d.size, 48)), d.kind) if d.kind != "bss" else ""))
        symbols._reindex()

        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(res.functions)} functions",
                              "mips", int(picked.big_endian), str(workdir), json.dumps(cfg))).lastrowid
            for fn in res.functions:
                target = match.target_from_split(fn, symbols.resolve, picked.big_endian, res.gp)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, fn.name, fn.size, len(fn.lines), target.code, json.dumps(target.to_json()),
                     self._asm_source(fn), None, fn.segment, fn.vram, fn.rom))
            for s in symbols.by_name.values():
                preview = next((r[4] for r in data_rows if r[0] == s.name), None)
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, s.name, s.address, s.size, s.kind, preview))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]) + (GP_NOTE if res.gp else ""),
                                                  None, "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(res.functions)} functions from {info['title']}")
        log(f"ready: {len(res.functions)} functions to decompile")
        return slug

    def _import_gba(self, picked, slug: str, workdir: Path, toolchain: str | None, flags: list[str] | None, log) -> str:
        """Game Boy Advance: functions found by luvdis (plus a pass for code nothing calls), judged by the ARM matcher."""
        from ..rom import gba

        info = picked.info
        fns = gba.split(workdir / picked.filename, workdir, log=log)
        toolchain = toolchain or "agbcc"
        tc = toolchains.get(toolchain)
        cfg = {
            "kind": "rom", "platform": "gba", "big_endian": False, "gp": 0, "arch": "thumb",
            "rom": {**info, "file": picked.filename, "program": picked.filename},
            "segments": [{"name": "rom", "rom_start": 0, "rom_end": info["size"], "vram": gba.BASE}],
            "compiler": {"toolchain": toolchain, "flags": flags or tc.flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(fns)} functions",
                              "arm", 0, str(workdir), json.dumps(cfg))).lastrowid
            names: set[str] = set()
            for f in fns:
                name = f.asm_only or f.name  # known hand-written helpers keep their real name and stay assembly
                if name in names:  # a game can link a helper twice (Spyro Orange has two of _call_via_r3)
                    name = f"{name}_{f.vram:08X}"
                names.add(name)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, len(f.code), f.n_insns, f.code, json.dumps([list(r) for r in f.refs or []]),
                     f.asm.replace(f.name, name), None, "rom", f.vram, f.rom, "asm" if f.asm_only else "open"))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, name, f.vram, len(f.code), "func", None))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]), None, "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(fns)} functions from {info['title']}")
        log(f"ready: {len(fns)} functions to decompile")
        return slug

    def rescan_gba(self, slug: str, *, apply: bool = False, revision: str | None = None, log=print) -> dict:
        """Preview edited functions.cfg; apply only against the exact preview revision.

        Affected targets and their full history are archived atomically; unchanged
        targets keep their IDs, matches, attempts and notes. No worker is started.
        """
        from .rescan import rescan_gba

        return rescan_gba(self, slug, apply=apply, revision=revision, log=log)

    def _require_not_rescanning(self, pid: int) -> None:
        if pid in self._rescanning:
            raise NetworkError("a GBA rescan is in progress; wait for it to finish before starting work")

    def _community_layout(self, data: bytes | Path, name: str = "") -> Path | None:
        """The file, when the game starts as a community-layout project: a GameCube/Wii program (DOL) or disc image
        (dtk-template), or a DS ROM (ds-decomp). Bytes are saved to a file first."""
        from ..forks import scratch_dsd, scratch_dtk

        head = Path(data).open("rb").read(0x440) if isinstance(data, (str, Path)) else bytes(data[:0x440])
        small = Path(data).read_bytes() if isinstance(data, (str, Path)) and Path(data).stat().st_size < 64 << 20 else data
        nds = scratch_dsd.is_nds(head)
        if not nds and scratch_dtk.disc_kind(head) is None and not (isinstance(small, (bytes, bytearray))
                                                                   and scratch_dtk.is_dol(small)):
            return None
        if isinstance(data, (str, Path)):
            return Path(data)
        uploads = self.workspace / ".decomp" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / (Path(name).name or ("game.nds" if nds else "main.dol" if scratch_dtk.is_dol(data) else "game.iso"))
        path.write_bytes(data)
        return path

    @staticmethod
    def _asm_source(fn: split.RomFunction) -> str:
        out = [f"glabel {fn.name}"]
        for ln in fn.lines:
            if ln.vram in fn.labels and ln.vram != fn.vram:
                out.append(f".L{ln.vram:08X}:")
            out.append(f"/* {ln.rom:X} {ln.vram:08X} {ln.word:08X} */  {ln.mnemonic:<10} {ln.operands}".rstrip())
        out.append(f"endlabel {fn.name}")
        return "\n".join(out)

    @staticmethod
    def _parse_asm(name: str, text: str, segment: str) -> split.RomFunction:
        lines, labels = [], set()
        for raw in text.splitlines():
            m = split._INSN.match(raw)
            if m:
                rom, vram, word, mnem, ops = m.groups()
                lines.append(split.AsmLine(int(rom, 16), int(vram, 16), int(word, 16), mnem, ops))
                continue
            m = split._LABEL.match(raw)
            if m:
                labels.add(int(m.group(1).lstrip(".L"), 16))
        return split.RomFunction(name, lines[0].vram, lines[0].rom, segment, lines, Path(), labels)

    # ------------------------------------------------------------------ drafts

    def _seed_for(self, fn, proj) -> str | None:
        """m2c draft, made the first time someone opens the function (imports stay fast)."""
        if fn["seed"] is not None or not seeds.available():
            return fn["seed"]
        tc = self.cfg(proj)["compiler"]["toolchain"]
        asm = self._asm_text(fn, proj)
        data_asm = None
        if "jtbl_" in fn["asm"]:
            data_asm = "\n".join(self._jtbl_asm(proj, fn))
        ctx = self._context(proj["id"], fn["unit"])
        draft = seeds.generate_from_asm(asm, fn["name"], tc, ctx["header"] if ctx else "", data_asm)
        text = f"/* machine-generated by m2c: a starting point, not a match */\n{draft}" if draft else ""
        with self.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text, fn["id"]))
        return text

    def _jtbl_asm(self, proj, fn) -> list[str]:
        out = [".section .rodata"]
        read = self.reader(proj)
        syms = self.symbols(proj["id"])
        for m in sorted(set(re.findall(r"jtbl_([0-9A-F]{8})", fn["asm"]))):
            addr = int(m, 16)
            sym = syms.by_addr.get(addr)
            size = sym.size if sym and sym.size else 0
            data = read(addr, size) if size else None
            if not data:
                continue
            out.append(f"dlabel {syms.label(addr)}")
            fmt = ">I" if self.cfg(proj).get("big_endian", True) else "<I"
            for (w,) in struct.iter_unpack(fmt, data[:len(data) // 4 * 4]):
                out.append(f".word .L{w:08X}" if fn["vram"] <= w < fn["vram"] + fn["size"] else f".word 0x{w:08X}")
        return out

    def get_context(self, project: str | None, function: str | None = None) -> str:
        self._rom_only(project, "get_context")
        return super().get_context(project, function)

    def _brief(self, fn, agent, header):
        if self.cfg(self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])).get("kind") == "fork":
            return self.forks.brief(fn, agent, header)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_rom(proj) and fn["status"] != "matched" and not fn["best_source"] and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(self._function(fn["name"], proj["slug"]), proj)
            fn = self._function(fn["name"], proj["slug"])
        return super()._brief(fn, agent, header)

    # ------------------------------------------------------------------ cross references

    def xrefs(self, proj) -> dict:
        """calls / called_by / uses / used_by, all keyed by address."""
        if proj["id"] in self._xrefs:
            return self._xrefs[proj["id"]]
        funcs = self.store.all("SELECT name, vram, size, relocs FROM functions WHERE project_id=?", proj["id"])
        starts = {f["vram"] for f in funcs}
        calls, called_by, uses, used_by = {}, {}, {}, {}
        for f in funcs:
            for off, rtype, addr in json.loads(f["relocs"]):
                if f["vram"] <= addr < f["vram"] + f["size"]:
                    continue
                if addr in starts:
                    calls.setdefault(f["vram"], set()).add(addr)
                    called_by.setdefault(addr, set()).add(f["vram"])
                elif rtype not in (match.R_MIPS_26, 10):  # 10: a Thumb call (GBA) to something not known as a function
                    uses.setdefault(f["vram"], set()).add(addr)
                    used_by.setdefault(addr, set()).add(f["vram"])
        self._xrefs[proj["id"]] = {"calls": calls, "called_by": called_by, "uses": uses, "used_by": used_by}
        return self._xrefs[proj["id"]]

    def _extra_brief(self, fn, proj) -> list[str]:
        if not self.is_rom(proj):
            return []
        syms = self.symbols(proj["id"])
        x = self.xrefs(proj)
        lines = ["", "### Where it lives",
                 f"address 0x{fn['vram']:08X}, ROM offset 0x{fn['rom_offset']:X}, segment {fn['unit']}"]
        calls = sorted(x["calls"].get(fn["vram"], ()))
        callers = sorted(x["called_by"].get(fn["vram"], ()))
        if calls:
            lines.append("calls: " + ", ".join(syms.label(a) for a in calls[:30]))
        if callers:
            lines.append("called by: " + ", ".join(syms.label(a) for a in callers[:30]))
        used = sorted(x["uses"].get(fn["vram"], ()))
        if used:
            lines += ["", "### Data it uses (declare what you need as extern in your source, or in context.h)"]
            prev = {r["name"]: r for r in self.store.all("SELECT name, kind, size, preview FROM symbols WHERE project_id=?", proj["id"])}
            for a in used[:40]:
                name, off = syms.name_at(a)
                r = prev.get(name)
                desc = f"{r['kind']}, {r['size']} bytes" if r else "unknown size"
                if r and r["preview"]:
                    desc += f": {r['preview']}"
                lines.append(f"- {syms.label(a)} (0x{a:08X}; {desc})")
        twin = self._twin_lines(proj, fn) if fn["status"] != "matched" else []
        lines += twin
        alike = [] if twin else [(r, sim) for r, sim in self.similar(proj, fn, top=3) if r["status"] == "matched"]
        if alike:
            lines += ["", "### Looks like (already matched; read with get_function)",
                      ", ".join(f"{r['name']} ({sim * 100:.0f}% similar)" for r, sim in alike)]
            r, sim = alike[0]
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], r["name"])
            if sim >= 0.8 and src and src["matched_source"] and len(src["matched_source"]) <= 3000:
                # nearly the same code: its matching C is the best starting point there is (saves a tool call)
                lines += [f"Matching C of {r['name']} ({sim * 100:.0f}% similar): start from this",
                          "```c", src["matched_source"].strip(), "```"]
        lines += ["", "Names: use the names shown here (or rename_symbol to give an address a real name). "
                      "Your code is linked at this function's address and must reproduce the ROM bytes exactly."]
        return lines

    def _rom_ops(self, proj) -> dict[tuple[int, int], list[str]]:
        """(vram, size) -> mnemonics of every function, cached (renames keep addresses; splits change sizes)."""
        cache = self._ops.setdefault(proj["id"], {})
        rows = self.store.all("SELECT vram, size, target FROM functions WHERE project_id=? AND target IS NOT NULL", proj["id"])
        live = set()
        for r in rows:
            key = (r["vram"], r["size"])
            live.add(key)
            if key not in cache:
                cache[key] = (similarity.thumb_mnemonics(r["target"], r["vram"] or 0) if proj["arch"] == "arm" else
                              similarity.mips_mnemonics(r["target"], bool(proj["big_endian"]), r["vram"] or 0))
        for key in set(cache) - live:
            del cache[key]
        return cache

    def similar(self, proj, fn, top: int = 5) -> list[tuple[dict, float]]:
        """Functions whose instructions look like `fn`'s, matched ones first: [(row, similarity)]."""
        if fn["target"] is None:
            return []
        ops = self._rom_ops(proj)
        me = (fn["vram"], fn["size"])
        if me not in ops:
            return []
        cands = {f"{v}:{n}": o for (v, n), o in ops.items() if (v, n) != me}
        rows = {(r["vram"], r["size"]): r for r in self.store.all(
            "SELECT name, vram, size, n_insns, status, best_score FROM functions WHERE project_id=?", proj["id"])}
        out = []
        for key, sim in similarity.rank(ops[me], cands, top=top * 2):
            v, n = (int(x) for x in key.split(":"))
            if (v, n) in rows:
                out.append((rows[(v, n)], sim))
        out.sort(key=lambda x: (x[0]["status"] != "matched", -x[1]))
        return out[:top]

    def find_similar(self, project: str | None, function: str, limit: int = 5) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.find_similar(fork, function, limit)
        proj = self._project(project)
        fn = self._function(function, proj["slug"])
        hits = self.similar(proj, fn, top=max(1, min(int(limit), 15)))
        if not hits:
            return f"No look-alikes found for {fn['name']}."
        lines = [f"Functions that look like {fn['name']}:"]
        for r, sim in hits:
            state = "matched" if r["status"] == "matched" else f"best {r['best_score']}" if r["best_score"] is not None else "not started"
            lines.append(f"- {r['name']}  {sim * 100:.0f}% similar  {state}  ({r['n_insns']} insns, 0x{r['vram']:08X})")
        best = next((r for r, _ in hits if r["status"] == "matched"), None)
        if best is not None:
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], best["name"])
            if src and src["matched_source"]:
                lines += ["", f"Matching C of {best['name']}:", "```c", src["matched_source"].strip()[:4000], "```"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ agent tools for ROMs

    def list_functions(self, project: str | None, status: str = "open", limit: int = 40) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.list_functions(fork, status, limit)
        proj = self._project(project)
        where = "" if status == "all" else " AND f.status=?"
        args = [proj["id"]] + ([] if status == "all" else [status])
        rows = self.store.all(
            f"SELECT f.name, f.vram, f.n_insns, f.status, f.best_score, a.name AS claimed_by FROM functions f "
            f"LEFT JOIN claims c ON c.function_id=f.id LEFT JOIN agents a ON a.id=c.agent_id "
            f"WHERE f.project_id=?{where} ORDER BY f.vram LIMIT ?", *args, limit)
        if not rows:
            return f"No {status} functions."
        return "\n".join(f"- {r['name']} @0x{r['vram']:08X}: {r['n_insns']} insns, {r['status']}"
                         + (f", best score {r['best_score']}" if r["best_score"] is not None else "")
                         + (f", claimed by {r['claimed_by']}" if r["claimed_by"] else "") for r in rows)

    def read_data(self, project: str | None, what: str, length: int = 64) -> str:
        self._rom_only(project, "read_data")
        proj = self._project(project)
        syms = self.symbols(proj["id"])
        m = _ADDRESS.match(what.strip())
        addr = int(m.group(1), 16) if m else syms.resolve(what.strip())
        if addr is None:
            raise NetworkError(f"`{what}` is not an address or a known symbol")
        length = max(4, min(int(length), 512))
        data = self.reader(proj)(addr, length)
        head = f"{syms.label(addr)} at 0x{addr:08X}"
        if data is None:
            sym = syms.by_addr.get(addr)
            if sym and sym.kind == "bss":
                return f"{head}: uninitialized data (.bss), {sym.size} bytes; it starts as zeros at boot."
            return f"{head}: not in a loaded part of the ROM (maybe .bss, or a segment that isn't split yet)."
        out = [head]
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            out.append(f"  {addr + i:08X}: {chunk.hex(' '):<47}  {text}")
        e = ">" if self.cfg(proj).get("big_endian", True) else "<"
        words = [w for (w,) in struct.iter_unpack(e + "I", data[:len(data) // 4 * 4])]
        ptrs = [f"+0x{4 * i:X}: {syms.label(w)}" for i, w in enumerate(words) if 0x80000400 <= w < 0x80800000]
        if ptrs:
            out.append("words that look like addresses: " + ", ".join(ptrs[:16]))
        floats = [struct.unpack(e + "f", struct.pack(e + "I", w))[0] for w in words[:8]]
        if any(1e-6 < abs(f) < 1e7 for f in floats):
            out.append("as f32: " + ", ".join(f"{f:g}" for f in floats))
        return "\n".join(out)

    def split_function(self, agent: Agent, project: str | None, function: str, at: str, new_name: str = "") -> str:
        with self.store.lock:
            pid = self._project(project)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._split_function_active(agent, project, function, at, new_name)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _split_function_active(self, agent, project, function, at, new_name):
        self._rom_only(project, "split_function")
        link = self._link(project)
        if link is not None:
            name = self._function(function, project)["name"]
            text = link.call("/collab/split", agent, function=name, at=at, new_name=new_name)
            if text.startswith("✅"):
                link.sync()
            return text
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("split_function is for ROM projects")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; it can't be split")
        m = _ADDRESS.match(str(at).strip())
        if not m:
            raise NetworkError("`at` must be an address like 0x80001234")
        addr = int(m.group(1), 16)
        new_name = (new_name or f"func_{addr:08X}").strip()
        syms = self.symbols(proj["id"])
        if not IDENT.match(new_name) or new_name in syms.by_name:
            raise NetworkError(f"`{new_name}` is not a free C identifier")
        if self.cfg(proj).get("platform") == "gba":
            return self._split_gba(agent, proj, fn, addr, new_name, syms)
        rf = self._parse_asm(fn["name"], fn["asm"], fn["unit"])
        try:
            head, tail = split.split_function(rf, addr, new_name)
        except ValueError as e:
            raise NetworkError(str(e)) from None
        syms.add(Symbol(new_name, addr, tail.size, "func"))
        old = syms.by_name.get(fn["name"])
        if old is not None:
            old.size = head.size
        th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL "
                       "WHERE id=?", (head.size, len(head.lines), th.code, json.dumps(th.to_json()),
                                      self._asm_source(head), fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, tail.size, len(tail.lines), tt.code,
                                                        json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                        fn["unit"], addr, tail.rom))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (head.size, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, tail.size, "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head.lines)} instructions; {new_name} at 0x{addr:08X} has "
                f"{len(tail.lines)}. Continue with {fn['name']} (get_function shows its new assembly).")

    def _split_gba(self, agent: Agent, proj, fn, addr: int, new_name: str, syms: SymbolTable) -> str:
        """Cut a GBA function's bytes and assembly in two at `addr` (Thumb: a halfword boundary)."""
        off = addr - fn["vram"]
        if off >= fn["size"] and off % 2 == 0:  # code after the function that nobody found yet: make it a function
            return self._add_gba_function(agent, proj, fn, addr, new_name, syms)
        if off <= 0 or off >= fn["size"] or off % 2:
            raise NetworkError(f"0x{addr:08X} isn't inside {fn['name']} (0x{fn['vram']:08X}, {fn['size']} bytes)")
        head, tail = fn["target"][:off], fn["target"][off:]
        cut = re.search(rf"^(?:_|\.L){addr:08X}:|^\t(?:thumb|arm)_func_start \w*{addr:08X}\b", fn["asm"], re.M)
        asm_head, asm_tail = ((fn["asm"][:cut.start()], fn["asm"][cut.start():]) if cut else
                              (fn["asm"], f"\tthumb_func_start {new_name}\n@ split from {fn['name']}: see its assembly\n"))
        refs = json.loads(fn["relocs"])
        refs_head = [r for r in refs if r[0] < off]
        refs_tail = [[r[0] - off, r[1], r[2]] for r in refs if r[0] >= off]
        syms.add(Symbol(new_name, addr, len(tail), "func"))
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL "
                       "WHERE id=?", (len(head), max(1, len(head) // 2), head, json.dumps(refs_head), asm_head, fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, len(tail), max(1, len(tail) // 2), tail,
                                                        json.dumps(refs_tail), asm_tail, None, fn["unit"], addr,
                                                        fn["rom_offset"] + off))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (len(head), proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, len(tail), "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head)} bytes; {new_name} at 0x{addr:08X} has {len(tail)}. "
                f"Continue with {fn['name']} (get_function shows its new assembly).")

    def _add_gba_function(self, agent: Agent, proj, prev, addr: int, new_name: str, syms: SymbolTable) -> str:
        from ..rom import gba, gba_candidates

        nxt = self.store.one("SELECT vram FROM functions WHERE project_id=? AND vram>? ORDER BY vram LIMIT 1",
                             proj["id"], prev["vram"])
        rom, _ = self._rom(proj)
        off, limit = addr - gba.BASE, (nxt["vram"] - gba.BASE) if nxt else len(rom)
        if not off < limit or not gba_candidates.plausible_function(rom, off):
            raise NetworkError(f"0x{addr:08X} doesn't look like the start of a function")
        literals = gba_candidates.literal_words(rom, [addr])
        end = min(next(gba_candidates.bodies(rom, [addr], literals))[1], limit)
        while end + 4 <= limit and end in literals:
            end += 4
        code = rom[off:end]
        syms.add(Symbol(new_name, addr, len(code), "func"))
        with self.store.tx() as db:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, len(code), max(1, len(code) // 2), code,
                                                        json.dumps(gba._refs(code, addr, literals, off)),
                                                        f"\tthumb_func_start {new_name}\n@ found by an agent after {prev['name']}\n",
                                                        None, prev["unit"], addr, off))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, len(code), "func"))
            self._event(db, proj["id"], agent.id, "split", f"{prev['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return f"✅ new function {new_name} at 0x{addr:08X}, {len(code)} bytes."

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        with self.store.lock:
            pid = self._project(project)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._rename_symbol_active(agent, project, old, new, message)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _rename_symbol_active(self, agent, project, old, new, message):
        self._rom_only(project, "rename_symbol")
        proj = self._project(project)
        if not self.is_rom(proj):
            return super().rename_symbol(agent, project, old, new, message)
        link = self._link(project)
        if link is not None:
            text = link.call("/collab/rename", agent, old=old, new=new, message=message)
            if text.startswith("✅"):
                link.sync()
            return text
        old, new = old.strip(), new.strip()
        if not (IDENT.match(old) and IDENT.match(new)):
            raise NetworkError("old and new must be C identifiers")
        syms = self.symbols(proj["id"])
        addr = syms.resolve(old)
        if addr is None:
            raise NetworkError(f"`{old}` is not a symbol of this ROM: use a name from the assembly (func_/D_...) "
                               f"or one given earlier")
        taken = syms.resolve(new)
        if taken is not None and taken != addr:
            raise NetworkError(f"`{new}` already names 0x{taken:08X}")
        word = re.compile(rf"\b{re.escape(old)}\b")
        trial = SymbolTable([Symbol(s.name, s.address, s.size, s.kind) for s in syms.by_name.values()])
        if old in trial.by_name:
            trial.rename(old, new)
        else:
            fnrow = self.store.one("SELECT size FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            current, _ = syms.name_at(addr)
            base = syms.by_name.get(current) if current != f"D_{addr:08X}" else None
            trial.add(Symbol(new, addr, fnrow["size"] if fnrow else (base.size if base and base.address == addr else 0),
                             "func" if fnrow else (base.kind if base else "data")))
        units = self._unit_contexts(proj["id"])
        current_ctx = {u: self._context(proj["id"], u) for u in ({""} | units)}
        changed = {u: word.sub(new, c["header"]) for u, c in current_ctx.items() if word.search(c["header"])}
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
        rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
        renamed_rows = [dict(r) | {"name": new if r["name"] == old else r["name"]} for r in matched]
        header_for = lambda fn: changed.get(self._effective_unit(fn, units),
                                            current_ctx[self._effective_unit(fn, units)]["header"])
        self._sym_override[proj["id"]] = trial
        try:
            broken = self.regression_test(proj, None, sources=rewritten, only=renamed_rows, header_for=header_for)
        finally:
            self._sym_override.pop(proj["id"], None)
        if broken:
            listing = "\n".join(f"- {n}: {why}" for n, why in broken[:10])
            return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
        msg = message.strip() or f"rename {old} -> {new}"
        with self.store.tx() as db:
            if db.execute("UPDATE symbols SET name=? WHERE project_id=? AND name=?", (new, proj["id"], old)).rowcount == 0:
                s = trial.by_name[new]
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (proj["id"], new, s.address, s.size, s.kind))
            db.execute("UPDATE functions SET name=? WHERE project_id=? AND (name=? OR vram=?)", (new, proj["id"], old, addr))
            for u, header in changed.items():
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
            for fid, src in rewritten.items():
                db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
            for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
            self._event(db, proj["id"], agent.id, "rename", f"{old} → {new}")
        self._syms.pop(proj["id"], None)
        return f"✅ 0x{addr:08X} is now `{new}` (was `{old}`); {len(rewritten)} matched sources updated, all still match."

    # ------------------------------------------------------------------ rooms (see collab.py)

    def link_project(self, slug: str, info: dict, client=None, start: bool = True):
        from .collab import Link

        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg["link"] = info
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        link = Link(self, slug, info["url"], info["room"], info["token"], info["member"], client=client)
        self.links[slug] = link
        if start:
            link.run()
        return link

    def restore_links(self) -> None:
        for p in self.store.all("SELECT slug, config FROM projects"):
            info = json.loads(p["config"]).get("link")
            if info and p["slug"] not in self.links:
                self.link_project(p["slug"], info)

    def unlink(self, slug: str) -> None:
        link = self.links.pop(slug, None)
        if link is not None:
            link.stop()
            try:
                link._req("POST", "/collab/leave", {})
            except NetworkError:
                pass
        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg.pop("link", None)
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    def _link(self, project: str | None):
        if not self.links:
            return None
        try:
            return self.links.get(self._project(project)["slug"])
        except NetworkError:
            return None

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest") -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.claim(agent, fork, function, strategy)
        link = self._link(project)
        if link is None:
            return super().claim(agent, project, function, strategy)
        if function:
            function = self._function(function, project)["name"]
        name = link.claim(agent, function, strategy)
        if name is None:
            return "Nothing left to claim in this room — every open function is matched or someone is on it. 🎉"
        link.sync()
        fn = self.claim_row(agent, project, name, strategy)
        return self._brief(fn, agent, header="CLAIMED")

    def claim_many(self, agent: Agent, project: str | None, count: int, strategy: str = "easiest") -> str:
        """Several small functions at once (count 2-6): the next one by `strategy` and, when it's small, its
        neighbours in the same file. Most small functions match on the first try, so an agent writes them all and
        judges them in one compile_and_diff: a fraction of the turns. Big functions come alone."""
        count = max(1, min(int(count or 1), 6))
        if count == 1 or self._link(project) is not None:
            return self.claim(agent, project, None, strategy)
        proj = self._project(project)
        held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                              "AND f.project_id=? ORDER BY f.vram, f.id", agent.id, proj["id"])
        if not held:
            text = self.claim(agent, project, None, strategy)  # the first, the usual way (with its "nothing left")
            held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                                  "AND f.project_id=?", agent.id, proj["id"])
            if not held or held[0]["n_insns"] > BATCH_INSNS:
                return text
        first = held[0]
        rows = list(held)
        if first["n_insns"] <= BATCH_INSNS and len(rows) < count:
            rows += self._batch_more(agent, proj, first, count - len(rows))
        if len(rows) == 1:
            return self.claim(agent, project, None, strategy)
        names = [r["name"] for r in rows]
        fork = self.fork_slug(project)
        head = (f"## CLAIMED {len(rows)} small functions: {', '.join(names)}\n"
                f"Write all of them, then judge them in one call: compile_and_diff(function=\"{', '.join(names)}\", "
                f"source=<all {len(rows)} definitions>). Each is judged on its own and saved as soon as it matches; "
                "then fix what's left (one at a time is fine). release_function any you can't do.")
        briefs = [self.forks.brief(r, agent, "CLAIMED") if fork else self._brief(r, agent, "CLAIMED") for r in rows]
        return "\n\n---\n\n".join([head, *briefs])

    def _batch_more(self, agent: Agent, proj, first, n: int) -> list:
        """Up to n more small open functions next to `first` (same file, nearest address), claimed for the agent."""
        now = time.time()
        with self.store.tx() as db:
            self._require_not_rescanning(proj["id"])
            self._expire_claims(db)
            rows = db.execute(
                "SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open' AND f.boost >= 0 AND f.n_insns <= ? "
                "AND f.abandons = 0 AND f.unit=? AND f.id NOT IN (SELECT function_id FROM claims) "
                "ORDER BY ABS(COALESCE(f.vram, 0) - ?), f.id LIMIT ?",
                (proj["id"], BATCH_INSNS, first["unit"], first["vram"] or 0, n)).fetchall()
            for r in rows:
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (r["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, proj["id"], agent.id, "claim", r["name"])
        return rows

    def attempt_many(self, agent: Agent, names: list[str], source: str, project: str | None = None) -> str:
        """compile_and_diff for several functions in one source: each judged on its own (a fork's, with the other
        definitions taken out, as its file gets them one by one), in order."""
        fork = self.fork_slug(project)
        out, matched = [], 0
        for name in names:
            try:
                one = self.forks.one_definition(fork, source, name, [n for n in names if n != name]) if fork else source
                res = self.attempt(agent, name, one, project)
            except NetworkError as e:
                res = {"text": f"❌ {e}", "matched": False}
            matched += bool(res.get("matched"))
            out.append(f"### {name}\n{res['text']}")
        return f"{matched} of {len(names)} matched.\n\n" + "\n\n".join(out)

    def _yield_autopilot(self, function: str | None, project: str | None) -> None:
        """The autopilot holds a function only while its draft compiles: an agent that wants it waits for that."""
        if not function:
            return
        try:
            fid = self._function(function, project)["id"]
        except NetworkError:
            return
        for _ in range(100):
            held = self.store.one("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id WHERE c.function_id=?", fid)
            if not held or held["name"] != "autopilot":
                return
            time.sleep(0.2)

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        # Include scratch compiles of matched functions, which deliberately have no claim.
        with self.store.lock:
            pid = self._function(function, project)["project_id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._attempt_active(agent, function, source, project)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _attempt_active(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        self._yield_autopilot(function, project)
        if (fork := self.fork_slug(project)):
            return self.forks.attempt(agent, fork, function, source)
        link = self._link(project)
        before = self._function(function, project)["best_score"] if link else None
        res = super().attempt(agent, function, source, project)
        if link is None or res["score"] is None:
            return res
        name = self._function(function, project)["name"]
        if res["matched"]:
            verdict = link.attempt(agent, name, source)
            if not verdict.get("matched"):
                res["text"] += ("\n⚠️ The room's host could not confirm this match on their copy (score "
                                f"{verdict.get('score')}). Check that both of you use the same compiler settings.")
            else:
                res["text"] += "\nShared with the room: the host confirmed the match."
        elif before is None or res["score"] < before:
            threading.Thread(target=lambda: self._report(link, agent, name, source, res["score"]), daemon=True).start()
        return res

    @staticmethod
    def _report(link, agent, name, source, score) -> None:
        try:
            link.call("/collab/best", agent, function=name, source=source, score=score)
        except NetworkError:
            pass

    def apply_reported_best(self, slug: str, name: str, score: int, source: str, agent: Agent) -> None:
        """Host side: a room member's better attempt, as they report it (their own compile scored it)."""
        fn = self._function(name, slug)
        if fn["status"] != "open" or score <= 0 or (fn["best_score"] is not None and score >= fn["best_score"]):
            return
        self._record(fn, agent, score, source, self._context(fn["project_id"], fn["unit"])["version"], via="room")

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.release(agent, fork, function, notes)
        link = self._link(project)
        if link is not None:
            link.call("/collab/release", agent, function=self._function(function, project)["name"], notes=notes)
        return super().release(agent, function, notes, project)

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        self._rom_only(project, "edit_context")
        link = self._link(project)
        if link is None:
            return super().edit_context(agent, project, old, new, message, function)
        text = link.call("/collab/context", agent, old=old, new=new, message=message, function=function)
        if text.startswith("✅"):
            link.sync()
        return text

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            return super().add_note(agent, project, text, function)
        name = self._function(function, project)["name"] if function else None
        return link.call("/collab/note", agent, text=text, function=name) or "Noted, and shared with the room."

    def _remote_agent(self, name: str) -> int:
        label = "@" + name
        if label in self._remote_ids:
            return self._remote_ids[label]
        row = self.store.one("SELECT id FROM agents WHERE name=?", label)
        if row is None:
            with self.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (label, "room", "remote:" + secrets.token_hex(16), time.time(), "remote")).lastrowid
        else:
            aid = row["id"]
        self._remote_ids[label] = aid
        return aid

    def remote_event(self, project_id: int, kind: str, detail: str, agent_name: str, created_at: float) -> None:
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (project_id, aid, kind, detail, created_at))

    def apply_remote_match(self, slug: str, vram: int, source: str, agent_name: str) -> None:
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status FROM functions WHERE project_id=? AND vram=?", (proj["id"], vram)).fetchone()
            if row is None or row["status"] == "matched":
                return
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=?, "
                       "best_score=0, best_source=? WHERE id=?", (source, aid, time.time(), source, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_match_named(self, slug: str, name: str, agent_name: str) -> None:
        """A git project's room: who matched a function (the code itself arrives with the host's commits)."""
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status, matched_agent_id FROM functions WHERE project_id=? AND name=?",
                             (proj["id"], name)).fetchone()
            if row is None:
                return
            if row["status"] != "matched":
                db.execute("UPDATE functions SET status='matched', matched_agent_id=?, matched_at=? WHERE id=?",
                           (aid, time.time(), row["id"]))
            elif row["matched_agent_id"] is None:
                db.execute("UPDATE functions SET matched_agent_id=? WHERE id=?", (aid, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_note_named(self, slug: str, function: str | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        row = self.store.one("SELECT id FROM functions WHERE project_id=? AND name=?", proj["id"], function) if function else None
        fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_best(self, slug: str, vram: int, score: int, source: str) -> None:
        proj = self._project(slug)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET best_score=?, best_source=? WHERE project_id=? AND vram=? AND status='open' "
                       "AND (best_score IS NULL OR best_score > ?)", (score, source, proj["id"], vram, score))

    def apply_remote_note(self, slug: str, host_id: int, vram: int | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        fid = None
        if vram is not None:
            row = self.store.one("SELECT id FROM functions WHERE project_id=? AND vram=?", proj["id"], vram)
            fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_context(self, slug: str, header: str, host_version: int) -> None:
        proj = self._project(slug)
        if self._context(proj["id"])["header"] == header:
            return
        with self.store.tx() as db:
            version = self._next_version(db, proj["id"])
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, None, f"from the room (host v{host_version})",
                                                  time.time(), ""))

    def apply_remote_layout(self, slug: str, functions: list[dict], symbols: list[dict]) -> None:
        """Mirror the room's splits and names. The host already regression-tested them on the same ROM."""
        proj = self._project(slug)
        pid = proj["id"]
        local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        for f in sorted(functions, key=lambda f: f["vram"]):
            if f["vram"] in local:
                continue
            owner = next((r for r in local.values() if r["vram"] < f["vram"] < r["vram"] + r["size"]), None)
            if owner is None or owner["status"] == "matched":
                continue
            rf = self._parse_asm(owner["name"], owner["asm"], owner["unit"])
            try:
                head, tail = split.split_function(rf, f["vram"], f["name"])
            except ValueError:
                continue
            syms = self.symbols(pid)
            syms.add(Symbol(f["name"], f["vram"], tail.size, "func"))
            th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
            with self.store.tx() as db:
                db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL WHERE id=?",
                           (head.size, len(head.lines), th.code, json.dumps(th.to_json()), self._asm_source(head), owner["id"]))
                db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, f["name"], tail.size, len(tail.lines), tt.code,
                                                            json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                            owner["unit"], f["vram"], tail.rom))
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, f["name"], f["vram"], tail.size, "func"))
            local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        self._xrefs.pop(pid, None)
        syms = self.symbols(pid)
        for sym in symbols:
            current, off = syms.name_at(sym["address"])
            if off == 0 and current == sym["name"]:
                continue
            old = current if off == 0 else f"D_{sym['address']:08X}"
            word = re.compile(rf"\b{re.escape(old)}\b")
            with self.store.tx() as db:
                db.execute("DELETE FROM symbols WHERE project_id=? AND (name=? OR name=?)", (pid, old, sym["name"]))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, sym["name"], sym["address"], sym["size"], sym["kind"]))
                db.execute("UPDATE functions SET name=? WHERE project_id=? AND vram=?", (sym["name"], pid, sym["address"]))
                for r in db.execute("SELECT id, matched_source, best_source FROM functions WHERE project_id=? AND "
                                    "(matched_source IS NOT NULL OR best_source IS NOT NULL)", (pid,)).fetchall():
                    db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?",
                               (word.sub(sym["name"], r["matched_source"]) if r["matched_source"] else None,
                                word.sub(sym["name"], r["best_source"]) if r["best_source"] else None, r["id"]))
            self._syms.pop(pid, None)
            syms = self.symbols(pid)

    # ------------------------------------------------------------------ export

    def export_rom(self, slug: str, log=print) -> dict:
        """Write a rebuildable source tree (the base for ports and mods) and prove it rebuilds the game."""
        if self.fork_slug(slug):  # a fork's work is already in its source tree, committed
            return {"path": self._project(slug)["root"], "identical": None}
        from ..rom.export import export_rom
        from ..rom.export_gba import export_gba

        proj = self._project(slug)
        export = export_gba if self.cfg(proj).get("platform") == "gba" else export_rom
        res = export(self, slug, Path(proj["root"]) / "source", log=log)
        with self.store.tx() as db:  # shown on the project page: what's in the tree and whether it rebuilds
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            cfg["export"] = {"t": time.time(), "path": str(res["path"]), "identical": res.get("identical"),
                             "matched": res.get("matched"), "functions": res.get("functions"),
                             "fallback": len(res.get("fallback") or [])}
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return res

    # ------------------------------------------------------------------ for the GUI

    def overview(self, slug: str) -> dict:
        proj = self._project(slug)
        last = self.store.one("SELECT MAX(created_at) AS t FROM events WHERE project_id=?", proj["id"])["t"]
        cfg = self.cfg(proj)
        # matches by the helper agents the app runs (their spend is what `spend` counts)
        helpers = self.store.one(
            "SELECT COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? "
            "AND f.status='matched' AND a.origin='local' AND a.name NOT IN ('you','autopilot')", proj["id"])["n"]
        twins = self.twins.jobs.get(proj["slug"])
        return self._overview(slug, proj) | {"last_activity": last, "autopilot": self.autopilot_status(slug),
                                             "permuter": self.permuter.status(proj["slug"]),
                                             "twins": None if twins is None else {
                                                 k: getattr(twins, k) for k in ("state", "pairs", "tried", "matched", "asm")},
                                             "helper_matches": helpers,
                                             "spend": cfg.get("spend"), "instructions": cfg.get("instructions") or "",
                                             "priority": cfg.get("priority", [])}

    def _overview(self, slug: str, proj) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.overview(fork)
        cfg = self.cfg(proj)
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
            "FROM functions WHERE project_id=?", proj["id"])
        tc = toolchains.get(cfg["compiler"]["toolchain"])
        return {
            "slug": proj["slug"], "title": proj["name"], "description": proj["description"],
            "platform": cfg.get("platform", "n64"), "platform_name": detect.PLATFORMS.get(cfg.get("platform", "n64"), ""),
            "compilers": PLATFORM_COMPILERS.get(cfg.get("platform", "n64"), []),
            "rom": {k: cfg["rom"].get(k) for k in ("title", "game_code", "region", "size", "sha1", "byte_order", "compiler_guess")},
            "compiler": {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cfg["compiler"]["flags"],
                         "installed": toolchains.installed(tc.id) is not None, "proprietary": tc.proprietary,
                         "supported": tc.supported, "wsl": tc.via_wsl,
                         "why": None if tc.supported else toolchains.unsupported_cpu()},
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "context_version": self._context(proj["id"])["version"],
            "folder": proj["root"],
            "export": cfg.get("export"),
            "exportable": cfg.get("platform", "n64") in ("n64", "psx", "ps2", "gba"),
            "room": self._room_info(proj["slug"]),
        }

    def _room_info(self, slug: str) -> dict | None:
        if slug in self.links:
            link = self.links[slug]
            return {"role": "guest", "host": link.url, "member": link.member, "error": link.error,
                    "members": link.members, "connected": time.time() - link.last_ok < 15}
        if slug in self.rooms:
            return {"role": "host", "members": self.rooms[slug].members()}
        return None

    def function_map(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT f.name, f.vram, f.size, f.n_insns, f.status, f.best_score, f.attempts, f.unit, f.matched_at, "
            "a.name AS agent, m.name AS matched_by FROM functions f LEFT JOIN claims c ON c.function_id=f.id "
            "LEFT JOIN agents a ON a.id=c.agent_id LEFT JOIN agents m ON m.id=f.matched_agent_id "
            "WHERE f.project_id=? ORDER BY f.vram, f.unit, f.id", proj["id"])
        link = self.links.get(slug)
        # who works on what in the room: by address, or by name in a git project (its functions have no addresses)
        key = (lambda r: r["name"]) if link is not None and link.fork else (lambda r: r["vram"])
        remote = {key(c): "@" + c["agent"] for c in (link.claims if link is not None else [])}
        return [{"name": r["name"], "vram": r["vram"], "size": r["size"], "insns": r["n_insns"], "unit": r["unit"],
                 "status": r["status"] if r["status"] in ("matched", "asm") else "working" if (r["agent"] or key(r) in remote) else "open",
                 "best": r["best_score"], "attempts": r["attempts"], "agent": r["agent"] or remote.get(key(r)),
                 "matched_at": r["matched_at"], "matched_by": r["matched_by"]} for r in rows]

    def map_signature(self, slug: str) -> str:
        """What the function map shows, summed up: it changes when a function is added, matched, tried or claimed.
        A few sums over indexed rows, so the GUI can ask "anything new?" every few seconds without fetching tens of
        thousands of functions (renames and splits come with events, which refetch anyway)."""
        proj = self._project(slug)
        f = self.store.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS hi, COALESCE(SUM(attempts), 0) AS at, "
                           "COALESCE(SUM(status='matched'), 0) AS m, COALESCE(SUM(best_score), 0) AS bs, "
                           "COALESCE(MAX(matched_at), 0) AS ma FROM functions WHERE project_id=?", proj["id"])
        c = self.store.one("SELECT COUNT(*) AS n, COALESCE(SUM(c.function_id * 31 + c.agent_id), 0) AS k FROM claims c "
                           "JOIN functions f ON f.id=c.function_id WHERE f.project_id=?", proj["id"])
        link = self.links.get(slug)
        remote = sorted(str(x.get("vram") or x.get("name")) + x.get("agent", "") for x in (link.claims if link else []))
        return f"{f['n']}.{f['hi']}.{f['at']}.{f['m']}.{f['bs']}.{f['ma']}.{c['n']}.{c['k']}.{hash(tuple(remote)) & 0xFFFFFF}"

    # ------------------------------------------------------------------ what agents spend, and steering them

    def _update_cfg(self, slug: str, change) -> dict:
        """Change a project's config in one transaction (change(cfg) edits it in place)."""
        proj = self._project(slug)
        with self.store.tx() as db:
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            change(cfg)
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return cfg

    def add_spend(self, slug: str, usd: float, tokens: int) -> None:
        if usd or tokens:
            def change(cfg):
                sp = cfg.setdefault("spend", {"usd": 0.0, "tokens": 0})
                sp["usd"] = round(sp.get("usd", 0) + usd, 6)
                sp["tokens"] = sp.get("tokens", 0) + tokens
            self._update_cfg(slug, change)

    def spend(self, slug: str) -> dict | None:
        return self.cfg(self._project(slug)).get("spend")

    def set_instructions(self, slug: str, text: str) -> None:
        """What the user wants agents to know or focus on, given to every agent on this project."""
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("instructions", text.strip()[:4000]))

    def instructions(self, slug: str) -> str:
        return self.cfg(self._project(slug)).get("instructions") or ""

    def set_priority(self, slug: str, name: str, on: bool = True) -> list[str]:
        """Functions the user wants done next: automatic claims take these first."""
        fn = self._function(name, slug)

        def change(cfg):
            items = [n for n in cfg.get("priority", []) if n != fn["name"]]
            cfg["priority"] = ([fn["name"]] + items) if on else items
        return self._update_cfg(slug, change)["priority"]

    def claim_row(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest"):
        self._yield_autopilot(function, project)
        with self.store.lock:
            proj = self._project(project) if project or function is None else self.store.one(
                "SELECT * FROM projects WHERE id=?", self._function(function, project)["project_id"])
            self._require_not_rescanning(proj["id"])
            return self._claim_row_available(agent, project, function, strategy)

    def _claim_row_available(self, agent, project, function, strategy):
        if function is None and project and not self.fork_slug(project):
            proj = self._project(project)
            for name in self.cfg(proj).get("priority", []):
                row = self.store.one("SELECT f.id, f.status FROM functions f WHERE f.project_id=? AND f.name=?", proj["id"], name)
                held = self.store.one("SELECT 1 FROM claims WHERE agent_id=?", agent.id)
                if row and row["status"] == "open" and not held and not self.store.one(
                        "SELECT 1 FROM claims WHERE function_id=?", row["id"]):
                    function = name
                    break
        return Network.claim_row(self, agent, project, function, strategy)

    # ------------------------------------------------------------------ autopilot

    def start_autopilot(self, slug: str, workers: int = 3) -> dict:
        """Try every open function's machine draft (m2c, compilable form) through the judge, smallest first, in the
        background. Whatever already matches is saved before any agent spends a token on it; drafts that compile
        become the function's best attempt, so agents start from them. Agents and autopilot never collide: a
        function is held only while its draft compiles, and claimed ones are skipped."""
        with self.store.lock:
            pid = self._project(slug)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._start_autopilot_available(slug, workers)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _start_autopilot_available(self, slug: str, workers: int) -> dict:
        state = self.autopilots.get(slug)
        if state and state["state"] == "running":
            return state
        # first the twins (identical code to something matched: every project), then machine drafts where m2c reads
        # the code and the project's headers are fit for them (ROM projects; games started from scratch)
        try:
            if (fork := self.fork_slug(slug)):  # a GameCube, Wii or DS game started from scratch: its own build judges
                ids = self.forks.autopilot_prepare(fork)
                try_one, workers = self.forks.autopilot_one, min(workers, 2)  # each try builds a file: fewer at once
            else:
                if not seeds.available():
                    raise NetworkError("autopilot's drafts need m2c, the draft decompiler: install it in Setup")
                proj = self._project(slug)
                tc = self.cfg(proj)["compiler"]["toolchain"]
                if not seeds.M2C_TARGETS.get(tc):
                    raise NetworkError("m2c can't read this project's code (no draft decompiler for it yet)")
                ids = [r["id"] for r in self.store.all("SELECT id FROM functions WHERE project_id=? AND status='open' "
                                                       "ORDER BY n_insns, vram", proj["id"])]
                try_one = self._autopilot_one
            no_drafts = ""
        except NetworkError as e:
            ids, try_one, no_drafts = [], None, str(e)
        state = {"state": "running", "total": len(ids), "tried": 0, "matched": 0, "near": 0, "twins": 0,
                 "phase": "twins", "no_drafts": no_drafts, "started_at": time.time(), "stop": threading.Event()}
        self.autopilots[slug] = state
        threading.Thread(target=self._autopilot_run, args=(slug, ids, state, workers, try_one),
                         daemon=True, name=f"autopilot-{slug}").start()
        return state

    def stop_autopilot(self, slug: str) -> None:
        if (state := self.autopilots.get(slug)) and state["state"] == "running":
            state["stop"].set()

    def autopilot_status(self, slug: str) -> dict | None:
        state = self.autopilots.get(slug)
        if state:
            return {k: v for k, v in state.items() if k != "stop"}
        last = self.cfg(self._project(slug)).get("autopilot")  # the last run, from before the app restarted
        return {"state": "done", **last} if last else None

    def _autopilot_run(self, slug: str, ids: list[int], state: dict, workers: int, try_one=None) -> None:
        from concurrent.futures import ThreadPoolExecutor

        agent, _ = self.local_agent("autopilot")
        proj = self._project(slug)
        try_one = try_one or self._autopilot_one
        try:  # identical code first: free, and it takes those functions off the drafts' list
            if slug not in self.links:  # (a room member's matches come from the host)
                job = self.twins.full_pass(slug)
                state["twins"] = job.matched
                state["matched"] += job.matched
        except Exception as e:
            logging.getLogger("decomp.wtf").info("autopilot twins: %s", e)
        state["phase"] = "drafts"
        ids = [fid for fid in ids if (r := self.store.one("SELECT status FROM functions WHERE id=?", fid)) and r["status"] == "open"]
        state["total"] = len(ids)

        def one(fid: int) -> None:
            if state["stop"].is_set():
                return
            try:
                outcome = try_one(agent, proj, fid)
            except Exception:  # one odd function must not stop the rest
                outcome = "fail"
            state["tried"] += 1
            if outcome in ("match", "near"):
                state[{"match": "matched", "near": "near"}[outcome]] += 1

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, ids))
        finally:
            state["state"] = "stopped" if state["stop"].is_set() else "done"
            state["finished_at"] = time.time()
            with self.store.tx() as db:
                cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
                cfg["autopilot"] = {k: state.get(k) for k in ("total", "tried", "matched", "near", "twins", "started_at",
                                                              "finished_at")}
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            drafts = state["matched"] - state.get("twins", 0)
            if drafts:  # (twins announce their own)
                with self.store.tx() as db:
                    self._event(db, proj["id"], agent.id, "autopilot",
                                f"{drafts} of {state['tried']} functions matched from machine drafts")

    def on_match(self, proj, function_id: int, agent: Agent | None = None) -> None:
        """Queue twin matches, or start a fresh autopilot pass after a manual match when enabled."""
        if proj is None:
            return
        if agent is not None and agent.name == "autopilot" and self.twins._full_running(proj["slug"]):
            return
        slug = proj["slug"]
        if (self.autopilot_after_match and agent is not None and agent.name != "autopilot"
                and slug not in self.links):
            with self._autopilot_restart_lock:
                state = self.autopilots.get(slug)
                if not state or state["state"] != "running":
                    self.start_autopilot(slug)
                    return
        self.twins.after_match(proj, function_id)

    def try_quietly(self, agent, proj, fn, source: str) -> dict:
        """Judge one candidate for a function nobody holds (autopilot's tries): held only while it compiles,
        recorded as an attempt like any other. {"matched", "score"}."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                raise NetworkError(f"{fn['name']} is claimed")
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + 300))
        try:
            ctx = self._context(proj["id"], fn["unit"])
            result = self._compile(proj, ctx["header"], source)
            if not result.ok:
                return {"matched": False, "score": None}
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                return {"matched": False, "score": None}
            d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match)
            return {"matched": d.bytes_match, "score": d.score}
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _autopilot_one(self, agent, proj, fid: int) -> str:
        """match | near | far | fail | skip, for one function's draft."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            fn = db.execute("SELECT * FROM functions WHERE id=?", (fid,)).fetchone()
            if fn is None or fn["status"] != "open" or db.execute("SELECT 1 FROM claims WHERE function_id=?", (fid,)).fetchone():
                return "skip"
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, now, now + 300))
        try:
            self._seed_for(fn, proj)  # the readable draft, for the agents that come after
            ctx = self._context(proj["id"], fn["unit"])
            data_asm = "\n".join(self._jtbl_asm(proj, fn)) if "jtbl_" in (fn["asm"] or "") else None
            draft = seeds.generate_from_asm(self._asm_text(fn, proj), fn["name"], self.cfg(proj)["compiler"]["toolchain"],
                                            ctx["header"], data_asm, valid=True)
            if not draft:
                return "fail"
            source = f"/* machine draft (m2c), tried by autopilot */\n{seeds.M2C_MACROS}\n{draft}\n"
            result = self._compile(proj, ctx["header"], source)
            if not result.ok:
                return "fail"
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                return "fail"
            d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match)
            return "match" if d.bytes_match else "near" if d.percent >= 90 else "far"
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fid, agent.id))

    def function_diff(self, slug: str, name: str, which: str = "latest") -> dict:
        """The target next to an attempt, instruction by instruction, for the GUI: the matched C, else the latest
        (or best) attempt, compiled and linked like the judge does. Cached, so watching an agent costs nothing."""
        if (fork := self.fork_slug(slug)):
            return self.forks.function_diff(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        latest = self.store.one("SELECT source FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        picks = [("matched", fn["matched_source"]), ("best", fn["best_source"]),
                 ("latest", latest["source"] if latest else None)]
        if which == "latest":
            picks = [picks[0], picks[2], picks[1]]
        kind, source = next(((k, v) for k, v in picks if v), (None, None))
        if source is None:
            return {"kind": None, "rows": None}
        ctx = self._context(proj["id"], fn["unit"])
        key = (fn["id"], hashlib.sha1(source.encode()).hexdigest(), ctx["version"], proj["config"])
        if key in self._diffs:
            return self._diffs[key] | {"kind": kind}
        result = self._compile(proj, ctx["header"], source)
        if not result.ok:
            out = {"rows": None, "error": result.log.strip()[-3000:] or "it didn't compile", "source": source}
        else:
            cand, verdict, problems = self._evaluate(fn, proj, result.obj)
            if cand is None:
                out = {"rows": None, "error": f"the object has no function named `{fn['name']}`", "source": source}
            else:
                d = scoring.diff(self._target_obj(fn, proj), cand)
                if verdict is not None:
                    d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
                out = {"rows": [list(r) for r in d.rows], "score": d.score,
                       "percent": 100.0 if d.bytes_match else min(99.9, int(d.percent * 10) / 10),
                       "problems": problems, "source": source}
        if len(self._diffs) > 128:
            self._diffs.pop(next(iter(self._diffs)))
        self._diffs[key] = out
        return out | {"kind": kind}

    def function_detail(self, slug: str, name: str) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.function_detail(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        attempts = self.store.all(
            "SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE t.function_id=? ORDER BY t.id", fn["id"])
        latest = self.store.one("SELECT source, score FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        notes = self.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                               "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
        x = self.xrefs(proj)
        syms = self.symbols(proj["id"])
        if not (fn["matched_source"] or fn["best_source"] or latest) and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(fn, proj)
            fn = self._function(fn["name"], slug)
        return {
            "name": fn["name"], "vram": fn["vram"], "rom_offset": fn["rom_offset"], "size": fn["size"],
            "insns": fn["n_insns"], "status": fn["status"], "best": fn["best_score"],
            "asm": self._asm_text(fn, proj),
            "source": fn["matched_source"] or fn["best_source"] or (latest["source"] if latest else None) or fn["seed"],
            "source_kind": "matched" if fn["matched_source"] else "best" if fn["best_source"] else
                           "latest" if latest else "draft" if fn["seed"] else None,
            "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
            "notes": [dict(n) for n in notes],
            "calls": [syms.label(a) for a in sorted(x["calls"].get(fn["vram"], ()))],
            "called_by": [syms.label(a) for a in sorted(x["called_by"].get(fn["vram"], ()))],
        }

    def stats(self, slug: str) -> dict:
        """Who did what and how fast: the overview's numbers (asked every few seconds, not every second)."""
        pid = self._project(slug)["id"]
        now = time.time()
        rows = self.store.all(
            "SELECT a.name AS agent, COUNT(t.id) AS compiles, MAX(t.created_at) AS last FROM attempts t "
            "JOIN functions f ON f.id=t.function_id JOIN agents a ON a.id=t.agent_id WHERE f.project_id=? GROUP BY a.id", pid)
        matched = {r["agent"]: r["n"] for r in self.store.all(
            "SELECT a.name AS agent, COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id "
            "WHERE f.project_id=? AND f.status='matched' GROUP BY a.id", pid)}
        board = [{"agent": r["agent"], "compiles": r["compiles"], "matched": matched.pop(r["agent"], 0), "last": r["last"]}
                 for r in rows]
        board += [{"agent": a, "compiles": 0, "matched": n, "last": None} for a, n in matched.items()]
        board.sort(key=lambda r: (-r["matched"], -r["compiles"]))
        per_hour = [0] * 24
        for r in self.store.all("SELECT matched_at FROM functions WHERE project_id=? AND status='matched' AND matched_at > ?",
                                pid, now - 86400):
            per_hour[23 - min(23, max(0, int((now - r["matched_at"]) // 3600)))] += 1  # (one matched since `now`: this hour)
        compiles = self.store.one("SELECT COUNT(*) AS n FROM attempts t JOIN functions f ON f.id=t.function_id "
                                  "WHERE f.project_id=? AND t.created_at > ?", pid, now - 3600)["n"]
        return {"agents": board[:24], "matches_per_hour": per_hour, "compiles_last_hour": compiles}

    def live(self, slug: str, since_event: int = 0, since_attempt: int = 0) -> dict:
        proj = self._project(slug)
        with self.store.tx() as db:
            self._expire_claims(db)
        events = self.store.all(
            "SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
            "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id DESC LIMIT 60", proj["id"], since_event)
        attempts = self.store.all(
            "SELECT t.id, t.score, t.compiled, t.created_at, f.name AS function, f.n_insns, a.name AS agent "
            "FROM attempts t JOIN functions f ON f.id=t.function_id LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE f.project_id=? AND t.id>? ORDER BY t.id DESC LIMIT 80", proj["id"], since_attempt)
        claims = self.store.all(
            "SELECT f.name, f.n_insns, f.best_score, f.attempts, a.name AS agent, c.claimed_at FROM claims c "
            "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id WHERE f.project_id=?", proj["id"])
        claims = [dict(c) for c in claims]
        if slug in self.links:
            for c in self.links[slug].claims:
                row = self.store.one("SELECT name, n_insns, best_score, attempts FROM functions WHERE project_id=? AND vram=?",
                                     proj["id"], c["vram"])
                if row:
                    claims.append(dict(row) | {"agent": "@" + c["agent"], "claimed_at": None})
        return {"events": [dict(e) for e in reversed(events)], "attempts": [dict(t) for t in reversed(attempts)],
                "claims": claims, "overview": self.overview(slug)}

    def set_compiler(self, slug: str, toolchain: str, flags: list[str] | None = None) -> None:
        if self.fork_slug(slug):
            raise NetworkError("a fork builds with the project's own compilers and flags")
        proj = self._project(slug)
        tc = toolchains.get(toolchain)
        cfg = self.cfg(proj)
        cfg["compiler"] = {"toolchain": tc.id, "flags": flags or tc.flags, "timeout": 20}
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            db.execute("UPDATE functions SET seed=NULL WHERE project_id=? AND status='open'", (proj["id"],))
            self._event(db, proj["id"], None, "compiler", f"{tc.name} {' '.join(cfg['compiler']['flags'])}")

    def chat_agent(self, base: str) -> tuple[Agent, str]:
        """An identity for an agent the user talks to (Claude Code, Codex, ... connected with `decomp mcp`): `base`,
        or base_2, base_3... when another session of the same agent is active right now (never base-2: that's the
        name of a helper session, see local_agent)."""
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-")[:30] or "chat"
        for n in range(1, 100):
            name = base if n == 1 else f"{base}_{n}"
            row = self.store.one("SELECT id, origin, last_seen FROM agents WHERE name=?", name)
            if row is None or (row["origin"] == "chat" and (row["last_seen"] or 0) < time.time() - 600):
                break
        else:
            raise NetworkError("too many chat sessions at once")
        if row is None:
            token = self.register_agent(name, "you", trusted=True, origin="chat")
        else:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=?, last_seen=? WHERE id=?", (_hash_token(token), time.time(), row["id"]))
        return self.authenticate(token), token

    def is_chat(self, agent: Agent) -> bool:
        row = self.store.one("SELECT origin FROM agents WHERE id=?", agent.id)
        return bool(row) and row["origin"] == "chat"

    def local_agent(self, name: str) -> tuple[Agent, str]:
        """An agent identity for one runner on this machine (created on first use). A name an agent the user talks
        to had before 0.10.4 (chat identities were numbered like helper sessions) becomes the runner's: a helper
        never gets the chat agent's tools, and the chat agent never takes over the runner's identity."""
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is not None:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=?, origin=CASE origin WHEN 'chat' THEN 'local' ELSE origin END "
                           "WHERE id=?", (_hash_token(token), row["id"]))
        else:
            token = self.register_agent(name, "you", trusted=True, origin="local")
        return self.authenticate(token), token
