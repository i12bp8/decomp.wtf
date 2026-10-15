"""Cfg-driven reconciliation without compiler downloads; only luvdis's subprocess is replaced."""

import json
import threading

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.rom import gba


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    rom = bytearray(0x200)
    rom[:4] = bytes.fromhex("2e0000ea")
    rom[0xB2] = 0x96
    rom[0xBD] = (-(sum(rom[0xA0:0xBD]) + 0x19)) & 255
    for off in (0x100, 0x104, 0x108, 0x110):
        rom[off:off + 4] = bytes.fromhex("00b500bd")
    (root / "baserom.gba").write_bytes(rom)
    (root / "functions.cfg").write_text("# authoritative starts\nthumb_func 0x08000100\n"
                                         "08000104 module.s added\nthumb_func 0x08000108 keep\n")
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "gba", "rom": {**gba.read_info(rom), "file": "baserom.gba"},
           "compiler": {"toolchain": "agbcc", "flags": [], "timeout": 20}, "priority": ["keep"],
           "segments": [{"name": "rom", "rom_start": 0, "rom_end": len(rom), "vram": gba.BASE}]}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES(?,?,?,?)",
                         ("game", "Game", str(root), json.dumps(cfg))).lastrowid
        for name, off, size in (("merged", 0x100, 8), ("keep", 0x108, 4), ("removed", 0x110, 4)):
            source = f"void {name}(void) {{}}"
            fid = db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,"
                             "status,matched_source,best_source,best_score,seed,attempts,meta) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (pid, name, size, size // 2, bytes(rom[off:off + size]), "[]",
                              f"\tthumb_func_start sub_{gba.BASE + off:08X}\n", "rom", gba.BASE + off, off,
                              "matched", source, source, 0, "draft", 1, '{"compiler":"custom"}')).lastrowid
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (pid, name, gba.BASE + off, size, "func"))
            db.execute("INSERT INTO attempts(function_id,score,source) VALUES(?,?,?)", (fid, 0, source))
            db.execute("INSERT INTO notes(project_id,function_id,text) VALUES(?,?,?)", (pid, fid, "important note"))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                   (pid, "g_data", gba.BASE + 0x180, 4, "data"))
        db.execute("INSERT INTO context_versions(project_id,version,header) VALUES(?,?,?)", (pid, 1, "original header"))
    calls = []

    def luvdis(rom_path, workdir, cfg_in, *, guess=True):
        assert not guess
        assert workdir != root and rom_path.parent == workdir
        text = cfg_in.read_text()
        assert "module.s" not in text and "added" not in text
        starts = gba.read_function_config(text, len(rom))
        calls.append(list(starts))
        # The cfg-out deliberately contains a removed start: it must never override input cfg.
        asm = "".join(f"\tthumb_func_start sub_{a:08X}\n\tpush {{lr}}\n\tpop {{pc}}\n" for a in starts)
        (workdir / "rom.s").write_text(asm)
        return asm, sorted(set(starts) | {gba.BASE + 0x110})

    monkeypatch.setattr(gba, "_luvdis", luvdis)
    return net, root, pid, calls


def test_preview_isolated_apply_preserves_unaffected_and_archives_history(project):
    net, root, pid, calls = project
    before = [dict(r) for r in net.store.all("SELECT * FROM functions ORDER BY id")]
    files = {p.name: p.read_bytes() for p in root.iterdir()}
    report = net.rescan_gba("game", log=lambda *_: None)
    assert report["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    assert not report["applied"] and report["revision"]
    assert [dict(r) for r in net.store.all("SELECT * FROM functions ORDER BY id")] == before
    assert {p.name: p.read_bytes() for p in root.iterdir()} == files
    # Populate caches to ensure apply invalidates them, including ROM and twins.
    net._syms[pid] = object()
    net._roms[pid] = object()
    net.twins._prints[pid] = object()
    applied = net.rescan_gba("game", apply=True, revision=report["revision"], log=lambda *_: None)
    assert applied["applied"] and len(calls) == 1  # apply executes the already-previewed plan
    kept = dict(net.store.one("SELECT * FROM functions WHERE name='keep'"))
    assert kept == before[1]
    assert net.store.one("SELECT source FROM attempts WHERE function_id=?", kept["id"])["source"] == kept["matched_source"]
    assert net.store.one("SELECT text FROM notes WHERE function_id=?", kept["id"])["text"] == "important note"
    changed = net.store.one("SELECT * FROM functions WHERE name='merged'")
    assert changed["id"] not in {r["id"] for r in before} and changed["status"] == "open"
    assert changed["size"] == 4 and changed["attempts"] == 0 and changed["matched_source"] is None
    assert not net.store.one("SELECT 1 FROM functions WHERE vram=?", gba.BASE + 0x110)
    archive = net.store.one("SELECT * FROM rescan_archives WHERE id=?", applied["archive_id"])
    saved = json.loads(archive["snapshot"])
    assert {r["name"] for r in saved["functions"]} == {"merged", "removed"}
    assert len(saved["attempts"]) == 2 and len(saved["notes"]) == 2
    assert bytes.fromhex(saved["functions"][0]["target"]["$bytes"]) == before[0]["target"]
    assert saved["functions"][0]["meta"] == before[0]["meta"]
    assert saved["context_versions"][0]["header"] == "original header"
    assert net.store.one("SELECT 1 FROM symbols WHERE name='g_data'")
    assert net.store.one("SELECT kind,address FROM symbols WHERE name='removed'")["kind"] == "data"
    assert applied["preserved_labels"] == ["removed"]
    assert pid not in net._syms and pid not in net._roms and pid not in net.twins._prints
    assert {p.name: p.read_bytes() for p in root.iterdir()} == files
    assert net.autopilots == {} and net.permuter.jobs == {} and net.twins.jobs == {}


@pytest.mark.parametrize("change", ["cfg", "rom", "map", "note", "config"])
def test_apply_requires_fresh_preview(project, change):
    net, root, pid, _ = project
    preview = net.rescan_gba("game", log=lambda *_: None)
    if change == "cfg":
        path = root / "functions.cfg"
        path.write_text(path.read_text() + "# edited\n")
    elif change == "rom":
        path = root / "baserom.gba"
        data = bytearray(path.read_bytes())
        data[-1] = 1
        path.write_bytes(data)
    else:
        with net.store.tx() as db:
            if change == "map":
                db.execute("UPDATE functions SET seed='new draft' WHERE name='keep'")
            elif change == "note":
                db.execute("UPDATE notes SET text='new note' WHERE project_id=?", (pid,))
            else:
                cfg = net.cfg(net.project_row("game"))
                cfg["instructions"] = "changed"
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    with pytest.raises(NetworkError, match="stale"):
        net.rescan_gba("game", apply=True, revision=preview["revision"])
    assert net.store.one("SELECT COUNT(*) AS n FROM functions")["n"] == 3


def test_no_apply_without_successful_preview(project):
    net, _, _, _ = project
    with pytest.raises(NetworkError, match="requires.*preview"):
        net.rescan_gba("game", apply=True)
    with pytest.raises(NetworkError, match="unavailable"):
        net.rescan_gba("game", apply=True, revision="invented")


@pytest.mark.parametrize("busy", ["claim", "scratch", "autopilot", "permuter", "twins", "session"])
def test_refuses_active_work_without_stopping_it(project, busy):
    net, _, pid, _ = project
    if busy == "claim":
        with net.store.tx() as db:
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (1, 1, 0, 0))
    elif busy == "scratch":
        net._active_attempts[pid] = 1
    elif busy == "autopilot":
        net.autopilots["game"] = {"state": "running", "stop": threading.Event()}
    elif busy == "session":
        net.rescan_busy = lambda slug: slug == "game"
    else:
        from types import SimpleNamespace
        getattr(net, busy).jobs["game"] = SimpleNamespace(state="running")
    with pytest.raises(NetworkError, match="active work or claims"):
        net.rescan_gba("game")
    if busy == "autopilot":
        assert not net.autopilots["game"]["stop"].is_set()


def test_reservation_blocks_claim_attempt_and_autopilot(project):
    net, _, pid, _ = project
    agent, _ = net.local_agent("tester")
    net._rescanning.add(pid)
    for work in (lambda: net.claim_row(agent, "game"), lambda: net.attempt(agent, "keep", "source", "game"),
                 lambda: net.start_autopilot("game"), lambda: net.rescan_gba("game"),
                 lambda: net.split_function(agent, "game", "merged", "0x08000104"),
                 lambda: net.rename_symbol(agent, "game", "keep", "renamed")):
        with pytest.raises(NetworkError, match="rescan is in progress"):
            work()
    assert not net.store.one("SELECT 1 FROM claims")


@pytest.mark.parametrize("text", ["", "# no starts\n", "garbage\n", "thumb_func 08000101\n",
                                  "thumb_func 08001000\n", "thumb_func 08000010\n",
                                  "thumb_func 08000100 bad-name\n", "arm_func 08000100\n",
                                  "08000100 duplicate\n08000104 duplicate\n", "08000100\n08000100\n"])
def test_rejects_empty_invalid_and_unsupported_cfg(project, text):
    net, root, _, calls = project
    (root / "functions.cfg").write_text(text)
    with pytest.raises(NetworkError, match="functions.cfg"):
        net.rescan_gba("game")
    assert not calls
    assert not net._rescanning


def test_missing_cfg_and_non_gba_errors(project):
    net, root, pid, _ = project
    (root / "functions.cfg").unlink()
    with pytest.raises(NetworkError, match="cannot read project functions.cfg"):
        net.rescan_gba("game")
    with net.store.tx() as db:
        cfg = net.cfg(net.project_row("game"))
        cfg["platform"] = "n64"
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    with pytest.raises(NetworkError, match="only for Game Boy Advance"):
        net.rescan_gba("game")


def test_rejects_moving_an_existing_name_to_another_address(project):
    net, root, _, _ = project
    (root / "functions.cfg").write_text("08000100 keep\n08000108 other\n")
    with pytest.raises(NetworkError, match="already labels a different ROM address"):
        net.rescan_gba("game", log=lambda *_: None)
    assert net.store.one("SELECT vram FROM functions WHERE name='keep'")["vram"] == gba.BASE + 0x108


def test_renaming_preserves_old_address_label_and_migrates_assignments(project):
    net, root, pid, _ = project
    with net.store.tx() as db:
        cfg = net.cfg(net.project_row("game"))
        cfg["priority"] = ["keep", "removed"]
        cfg["compiler_functions"] = {"keep": "my-profile", "removed": "old-profile"}
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    path = root / "functions.cfg"
    path.write_text(path.read_text().replace("08000108 keep", "08000108 renamed"))
    report = net.rescan_gba("game", log=lambda *_: None)
    net.rescan_gba("game", apply=True, revision=report["revision"], log=lambda *_: None)
    assert net.store.one("SELECT kind,address FROM symbols WHERE name='keep'")["kind"] == "data"
    assert net.store.one("SELECT address FROM symbols WHERE name='keep'")["address"] == gba.BASE + 0x108
    cfg = net.cfg(net.project_row("game"))
    assert cfg["priority"] == ["renamed"]
    assert cfg["compiler_functions"] == {"renamed": "my-profile"}


def test_scratch_compile_without_claim_blocks_rescan(project, monkeypatch):
    net, _, pid, _ = project
    started, release = threading.Event(), threading.Event()
    agent, _ = net.local_agent("scratch")
    results = []

    def compile_scratch(*args):
        started.set()
        release.wait(5)
        return {"score": 1}

    monkeypatch.setattr(net, "_attempt_active", compile_scratch)
    worker = threading.Thread(target=lambda: results.append(net.attempt(agent, "keep", "source", "game")))
    worker.start()
    try:
        assert started.wait(5)
        assert not net.store.one("SELECT 1 FROM claims")
        with pytest.raises(NetworkError, match="active work"):
            net.rescan_gba("game")
    finally:
        release.set()
        worker.join(5)
    assert results == [{"score": 1}] and net._active_attempts[pid] == 0


def test_missing_disassembly_block_rejects_plan(project, monkeypatch):
    net, _, _, _ = project
    monkeypatch.setattr(gba, "_luvdis", lambda *a, **kw: ("", []))
    with pytest.raises(NetworkError, match="could not analyze configured starts"):
        net.rescan_gba("game", log=lambda *_: None)
    assert not net._rescanning


def test_archive_and_map_changes_rollback_together(project):
    net, _, _, _ = project
    preview = net.rescan_gba("game", log=lambda *_: None)
    before = [dict(r) for r in net.store.all("SELECT * FROM functions ORDER BY id")]
    attempts = [dict(r) for r in net.store.all("SELECT * FROM attempts ORDER BY id")]
    with net.store.tx() as db:
        db.execute("CREATE TRIGGER refuse_rescan_insert BEFORE INSERT ON functions "
                   "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
        net.rescan_gba("game", apply=True, revision=preview["revision"], log=lambda *_: None)
    assert [dict(r) for r in net.store.all("SELECT * FROM functions ORDER BY id")] == before
    assert [dict(r) for r in net.store.all("SELECT * FROM attempts ORDER BY id")] == attempts
    assert not net.store.one("SELECT 1 FROM sqlite_master WHERE name='rescan_archives'")
    assert not net._rescanning


def test_project_deletion_cascades_private_archives(project):
    net, _, pid, _ = project
    preview = net.rescan_gba("game", log=lambda *_: None)
    net.rescan_gba("game", apply=True, revision=preview["revision"], log=lambda *_: None)
    with net.store.tx() as db:
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
    assert not net.store.one("SELECT 1 FROM rescan_archives")


@pytest.mark.parametrize("mutation", ["split", "rename"])
def test_inflight_map_mutation_blocks_rescan(project, monkeypatch, mutation):
    net, _, pid, _ = project
    agent, _ = net.local_agent("editing")
    started, release = threading.Event(), threading.Event()
    results = []

    def edit_map(*args):
        started.set()
        release.wait(5)
        return "edited"

    if mutation == "split":
        monkeypatch.setattr(net, "_split_function_active", edit_map)
        work = lambda: net.split_function(agent, "game", "merged", "0x08000104")
    else:
        monkeypatch.setattr(net, "_rename_symbol_active", edit_map)
        work = lambda: net.rename_symbol(agent, "game", "keep", "renamed")
    worker = threading.Thread(target=lambda: results.append(work()))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(NetworkError, match="active work"):
            net.rescan_gba("game")
    finally:
        release.set()
        worker.join(5)
    assert results == ["edited"] and net._active_attempts[pid] == 0


@pytest.mark.parametrize("existing_floor", [0, 100])
def test_archival_preserves_global_attempt_id_cursor(project, existing_floor):
    net, _, _, _ = project
    before = net.store.one("SELECT MAX(id) AS n FROM attempts")["n"]
    if existing_floor:
        with net.store.tx() as db:
            db.execute("INSERT INTO settings(key,value) VALUES('attempt_id_floor',?)", (str(existing_floor),))
    preview = net.rescan_gba("game", log=lambda *_: None)
    net.rescan_gba("game", apply=True, revision=preview["revision"], log=lambda *_: None)
    assert net.store.one("SELECT MAX(id) AS n FROM attempts")["n"] < before
    floor = int(net.store.one("SELECT value FROM settings WHERE key='attempt_id_floor'")["value"])
    assert floor == max(before, existing_floor)
    agent, _ = net.local_agent("next-compile")
    fn = net._function("merged", "game")
    net._record(fn, agent, 1, "new source", 1)
    after = net.store.one("SELECT MAX(id) AS n FROM attempts")["n"]
    assert after > max(before, existing_floor)
