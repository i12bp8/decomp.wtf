"""Explicit GBA cfg reconciliation, with preview revisions and transactional history archives.

rescan_archives.snapshot is JSON: complete affected function rows, attempts, notes,
twin misses, project configuration, context versions and symbols. BLOB values use
{"$bytes": "<hex>"}. Archived scores describe the old target, never a new one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import time
from pathlib import Path

from ..network import NetworkError
from ..rom import gba


def _row(row) -> dict:
    return {k: {"$bytes": v.hex()} if isinstance(v, bytes) else v for k, v in dict(row).items()}


def _rows(net, table: str, pid: int) -> list[dict]:
    return [_row(r) for r in net.store.all(f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", pid)]


def _inputs(net, proj):
    cfg = net.cfg(proj)
    root = Path(proj["root"])
    try:
        config = (root / "functions.cfg").read_bytes()
    except OSError as e:
        raise NetworkError(f"cannot read project functions.cfg: {e}") from None
    try:
        text = config.decode("utf-8")
    except UnicodeError:
        raise NetworkError("functions.cfg must be valid UTF-8") from None
    try:
        rom = (root / cfg["rom"]["file"]).read_bytes()
    except OSError as e:
        raise NetworkError(f"cannot read project GBA ROM: {e}") from None
    try:
        info = gba.read_info(rom)
        gba.read_function_config(text, len(rom))
    except gba.RomError as e:
        raise NetworkError(str(e)) from None
    if info["sha1"] != cfg["rom"].get("sha1"):
        raise NetworkError("project ROM no longer matches the imported SHA-1; preview is stale and rescan is refused")
    state = {"project": _row(proj), "functions": _rows(net, "functions", proj["id"]),
             "symbols": _rows(net, "symbols", proj["id"]),
             "context_versions": _rows(net, "context_versions", proj["id"]),
             "notes": _rows(net, "notes", proj["id"])}
    digest = hashlib.sha256()
    for value in (rom, config, json.dumps(state, sort_keys=True, separators=(",", ":")).encode()):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return rom, text, digest.hexdigest()


def _idle(net, proj):
    slug, pid = proj["slug"], proj["id"]
    if slug in net.links or slug in net.rooms:
        raise NetworkError("leave or close the collaboration room before rescanning this project")
    busy = net._active_attempts.get(pid, 0) or net.rescan_busy(slug)
    busy = busy or net.autopilots.get(slug, {}).get("state") == "running"
    busy = busy or any(getattr(jobs.get(slug), "state", None) == "running"
                       for jobs in (net.permuter.jobs, net.twins.jobs))
    with net.twins._lock:
        busy = busy or pid in net.twins._busy or bool(net.twins._queue.get(pid))
    held = net.store.one("SELECT 1 FROM claims c JOIN functions f ON f.id=c.function_id "
                         "WHERE f.project_id=? LIMIT 1", pid)
    if busy or held:
        raise NetworkError("project has active work or claims; stop workers/autopilot and release claims before rescanning")


def _plan(net, proj, fns, text):
    configured = gba.read_function_config(text, net.cfg(proj)["rom"]["size"])
    old = {r["vram"]: r for r in net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])}
    if len(old) != len(net.store.all("SELECT id FROM functions WHERE project_id=?", proj["id"])):
        raise NetworkError("existing function map has duplicate addresses; cannot safely rescan")
    details = {k: [] for k in ("added", "removed", "changed", "unchanged")}
    names = set()
    for fn in fns:
        before = old.get(fn.vram)
        name = configured[fn.vram] or (before["name"] if before else fn.asm_only or fn.name)
        if name in names:
            raise NetworkError(f"functions.cfg resolves to duplicate function name {name!r}; name the starts explicitly")
        names.add(name)
        fn.asm = re.sub(rf"\b{re.escape(fn.name)}\b", name, fn.asm)
        fn.name = name
        item = {"address": f"0x{fn.vram:08X}", "name": name, "size": len(fn.code)}
        same = (before is not None and before["name"] == name and before["target"] == fn.code
                and before["size"] == len(fn.code)
                and ("arm_func_start" not in (before["asm"] or "")) == fn.thumb
                and json.loads(before["relocs"] or "[]") == [list(r) for r in fn.refs or []])
        category = "added" if before is None else "unchanged" if same else "changed"
        if before:
            item["id"] = before["id"]
            item["previous"] = {"name": before["name"], "size": before["size"], "status": before["status"],
                                "attempts": before["attempts"], "has_source": bool(before["matched_source"] or before["best_source"] or before["seed"])}
        details[category].append(item)
    found = {f.vram for f in fns}
    for addr, before in old.items():
        if addr not in found:
            details["removed"].append({"id": before["id"], "address": f"0x{addr:08X}", "name": before["name"],
                                       "size": before["size"], "status": before["status"], "attempts": before["attempts"]})
    # Existing source must never silently resolve the same name at a new address.
    desired = {f.name: f.vram for f in fns}
    for s in net.store.all("SELECT name,address FROM symbols WHERE project_id=?", proj["id"]):
        if s["name"] in desired and s["address"] != desired[s["name"]]:
            raise NetworkError(f"configured function name {s['name']!r} already labels a different ROM address")
    labels = [r["name"] for r in details["removed"]]
    labels += [r["previous"]["name"] for r in details["changed"] if r["previous"]["name"] != r["name"]]
    return {"slug": proj["slug"], "applied": False, "counts": {k: len(v) for k, v in details.items()},
            "details": details, "preserved_labels": labels}


def _apply(net, db, proj, fns, report):
    pid = proj["id"]
    affected = [r["id"] for k in ("changed", "removed") for r in report["details"][k]]
    archive = None
    if affected:
        ids = set(affected)
        snapshot = {"format": 1, "project": _row(proj),
                    "functions": [r for r in _rows(net, "functions", pid) if r["id"] in ids],
                    "context_versions": _rows(net, "context_versions", pid), "symbols": _rows(net, "symbols", pid),
                    "events": _rows(net, "events", pid)}
        for table in ("attempts", "notes", "twin_misses"):
            snapshot[table] = [_row(r) for r in db.execute(
                f"SELECT t.* FROM {table} t JOIN functions f ON f.id=t.function_id WHERE f.project_id=?", (pid,))
                if r["function_id"] in ids]
        db.execute("CREATE TABLE IF NOT EXISTS rescan_archives(id INTEGER PRIMARY KEY, "
                   "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
                   "revision TEXT NOT NULL, created_at REAL NOT NULL, snapshot TEXT NOT NULL)")
        archive = db.execute("INSERT INTO rescan_archives(project_id,revision,created_at,snapshot) VALUES(?,?,?,?)",
                             (pid, report["revision"], time.time(), json.dumps(snapshot))).lastrowid
        # The live feed uses attempt IDs as a cursor. Archival must not allow
        # SQLite to reuse a deleted highest ID on the next compile.
        maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM attempts").fetchone()[0]
        db.execute("INSERT INTO settings(key,value) VALUES('attempt_id_floor',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(settings.value AS INTEGER),CAST(excluded.value AS INTEGER))",
                   (str(maximum),))
    next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM functions").fetchone()[0]
    for fid in affected:
        row = db.execute("SELECT name FROM functions WHERE id=?", (fid,)).fetchone()
        # The bytes still exist in the ROM. Keep their labels for unaffected C
        # callers/pointers, but do not expose removed targets as live functions.
        db.execute("UPDATE symbols SET kind='data' WHERE project_id=? AND name=? AND kind='func'", (pid, row["name"]))
        for table in ("attempts", "notes", "twin_misses"):
            db.execute(f"DELETE FROM {table} WHERE function_id=?", (fid,))
        db.execute("DELETE FROM functions WHERE id=?", (fid,))
    unchanged = {int(r["address"], 16) for r in report["details"]["unchanged"]}
    for fn in fns:
        if fn.vram in unchanged:
            continue
        db.execute("INSERT INTO functions(id,project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (next_id, pid, fn.name, len(fn.code), fn.n_insns, fn.code, json.dumps(fn.refs or []), fn.asm,
                    "rom", fn.vram, fn.rom, "asm" if fn.asm_only else "open"))
        next_id += 1
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(project_id,name) DO UPDATE SET size=excluded.size,kind=excluded.kind",
                   (pid, fn.name, fn.vram, len(fn.code), "func"))
    cfg = net.cfg(proj)
    names = {fn.name for fn in fns}
    renames = {r["previous"]["name"]: r["name"] for r in report["details"]["changed"]}
    if "priority" in cfg:
        cfg["priority"] = list(dict.fromkeys(renames.get(n, n) for n in cfg["priority"] if renames.get(n, n) in names))
    if "compiler_functions" in cfg:
        cfg["compiler_functions"] = {renames.get(n, n): p for n, p in cfg["compiler_functions"].items()
                                     if renames.get(n, n) in names}
    description = proj["description"] or ""
    description = re.sub(r"\d+ functions\b", f"{len(fns)} functions", description)
    db.execute("UPDATE projects SET config=?,description=? WHERE id=?", (json.dumps(cfg), description, pid))
    net._event(db, pid, None, "rescan", json.dumps({"counts": report["counts"], "archive_id": archive}))
    return archive


def rescan_gba(net, slug: str, *, apply: bool = False, revision: str | None = None, log=print) -> dict:
    with net.store.lock:
        proj = net._project(slug)
        if not net.is_rom(proj) or net.cfg(proj).get("platform") != "gba":
            raise NetworkError("rescan is supported only for Game Boy Advance ROM projects")
        net._require_not_rescanning(proj["id"])
        _idle(net, proj)
        if apply and not revision:
            raise NetworkError("apply requires the revision from a successful rescan preview")
        net._rescanning.add(proj["id"])
    try:
        with net.store.lock:
            rom, text, current = _inputs(net, proj)
            cached = net._rescan_previews.get(slug)
            if apply and (not cached or revision != cached[0] or revision != current):
                raise NetworkError("rescan preview is stale or unavailable; preview again before applying")
        if not apply:
            with tempfile.TemporaryDirectory(prefix="decomp-gba-rescan-") as directory:
                work = Path(directory)
                image = work / "input.gba"
                image.write_bytes(rom)
                try:
                    fns = gba.split_config(image, work, text, log=log)
                except gba.RomError as e:
                    raise NetworkError(str(e)) from None
            if not fns:
                raise NetworkError("rescan produced an empty function map; refusing to apply")
            with net.store.lock:
                _idle(net, proj)
                if _inputs(net, net._project(slug))[2] != current:
                    raise NetworkError("ROM, functions.cfg or function map changed during analysis; preview again")
                report = _plan(net, proj, fns, text)
                report["revision"] = current
                net._rescan_previews[slug] = (current, fns, report)
                return copy.deepcopy(report)
        _, fns, preview = cached
        report = copy.deepcopy(preview)
        with net.store.tx() as db:
            proj = net._project(slug)
            _idle(net, proj)
            if _inputs(net, proj)[2] != revision:
                raise NetworkError("ROM, functions.cfg or function map changed; preview again before applying")
            report["archive_id"] = _apply(net, db, proj, fns, report)
        pid = proj["id"]
        with net.store.lock:
            for cache in (net._syms, net._sym_override, net._roms, net._xrefs, net._ops):
                cache.pop(pid, None)
            net._diffs.clear()
            with net.twins._lock:
                net.twins._prints.pop(pid, None)
            net._rescan_previews.pop(slug, None)
        report["applied"] = True
        log(f"rescan applied: {report['counts']}; history archive {report['archive_id']}")
        return report
    finally:
        with net.store.lock:
            net._rescanning.discard(proj["id"])
