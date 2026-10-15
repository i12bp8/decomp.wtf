"""PlayStation overlays (rom/overlays.py): code in other files of a disc, placed at the address it's loaded at from
its own calls, and split with the program, as a segment of its own."""

import struct
from pathlib import Path

import pytest

from decompnet.rom import detect, overlays, split

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "projects" / "demo-psx" / "build" / "DEMOQ_PS1.EXE"
NOP, JR_RA = 0, 0x03E00008


def _overlay(base: int, count: int) -> bytes:
    """`count` little functions at `base` (little-endian), each calling the next."""
    words = []
    for i in range(count):
        callee = base + ((i + 1) % count) * 32
        words += [0x27BDFFE8, 0xAFBF0014, 0x0C000000 | ((callee >> 2) & 0x03FFFFFF), NOP, 0x8FBF0014, 0x27BD0018,
                  JR_RA, NOP]
    return struct.pack(f"<{len(words)}I", *words)


def test_an_overlay_is_placed_at_its_load_address():
    data = bytes(64) + _overlay(0x80100040, 60)  # a little header before its code, as overlay files often have
    assert overlays.place(data, "psx", [(0x80010000, 0x80050000)]) == (0x80100000, 64)
    assert overlays.place(data, "psx", [(0x800F0000, 0x80120000)]) is None  # over the resident program: no


def test_ps2_elf_overlays_say_where_they_go():
    code = _overlay(0x00400000, 40)
    phoff = 52
    elf = bytearray(b"\x7fELF\x01\x01\x01" + bytes(9))
    elf += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, 0x00400000, phoff, 0, 0, 52, 32, 1, 0, 0, 0)
    elf += struct.pack("<8I", 1, 0x100, 0x00400000, 0x00400000, len(code), len(code), 5, 0x10)
    elf += bytes(0x100 - len(elf)) + code
    assert overlays._elf_segments(bytes(elf)) == [(0x00400000, code)]
    irx = bytearray(elf)
    irx[16:18] = struct.pack("<H", 0xFF80)  # an IOP module: relocatable, left out
    assert overlays._elf_segments(bytes(irx)) is None


@pytest.mark.skipif(not EXE.exists(), reason="needs the PlayStation demo (projects/demo-psx)")
def test_overlays_are_split_with_the_program(tmp_path):
    (tmp_path / "game.exe").write_bytes(EXE.read_bytes())
    # two overlays that load at the same address (only one is in memory at a time)
    found = [("battle", _overlay(0x80180000, 30), 0x80180000, 0), ("field", _overlay(0x80180000, 20), 0x80180000, 0)]
    res = split.split_rom(tmp_path, "game.exe", log=lambda *_: None, overlays=found)
    names = {f.name for f in res.functions}
    assert "func_80180000" in names or "ovl_battle_func_80180000" in names
    battle = [f for f in res.functions if f.segment == "ovl_battle"]
    field = [f for f in res.functions if f.segment == "ovl_field"]
    assert len(battle) == 30 and len(field) == 20
    assert len({f.name for f in battle + field}) == 50  # same addresses, names of their own
    assert res.image == "game.overlays.exe"


def _iso(files: dict[str, bytes]) -> bytes:
    """The smallest ISO 9660 image the disc reader takes: a volume descriptor, a root directory, the files."""
    def both16(v):
        return struct.pack("<H", v) + struct.pack(">H", v)

    def both32(v):
        return struct.pack("<I", v) + struct.pack(">I", v)

    def record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
        rec = bytes([0, 0]) + both32(lba) + both32(size) + bytes(7) + bytes([2 if is_dir else 0, 0, 0]) + both16(1) \
            + bytes([len(name)]) + name
        rec += bytes(len(rec) % 2)
        return bytes([len(rec)]) + rec[1:]

    lba = 19
    places = {}
    for name, data in files.items():
        places[name] = (lba, len(data))
        lba += -(-len(data) // 2048)
    root = record(b"\0", 18, 2048, True) + record(b"\1", 18, 2048, True)
    for name, (at, size) in places.items():
        root += record(f"{name};1".encode(), at, size, False)
    pvd = bytearray(2048)
    pvd[0:7] = b"\x01CD001\x01"
    pvd[156:156 + 34] = record(b"\0", 18, 2048, True)
    image = bytearray(16 * 2048) + pvd + bytearray(b"\xffCD001\x01".ljust(2048, b"\0")) + bytearray(root.ljust(2048, b"\0"))
    for name, data in files.items():
        image += data + bytes(-len(data) % 2048)
    return bytes(image)


@pytest.mark.skipif(not EXE.exists(), reason="needs the PlayStation demo (projects/demo-psx)")
def test_a_disc_with_an_overlay_imports_both(tmp_path):
    from decompnet.app.engine import RomNetwork

    disc = _iso({"SYSTEM.CNF": b"BOOT = cdrom:\\DEMOQ_PS1.EXE;1\r\n", "DEMOQ_PS1.EXE": EXE.read_bytes(),
                 "BATTLE.BIN": bytes(64) + _overlay(0x80180040, 40), "MUSIC.VAB": bytes(range(256)) * 32})
    (tmp_path / "game.iso").write_bytes(disc)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    said = []
    slug = net.import_rom(tmp_path / "game.iso", confirmed=True, log=said.append)
    assert any("overlay on the disc: battle at 0x80180000" in s for s in said), said
    rows = net.store.all("SELECT name, vram FROM functions WHERE project_id=?", net.project_row(slug)["id"])
    in_overlay = [r for r in rows if 0x80180000 <= r["vram"] < 0x80181000]
    assert len(in_overlay) == 40 and len(rows) > 40  # the program's functions and the overlay's


def _ps2_elf(base: int, count: int = 24) -> bytes:
    code = _overlay(base, count)
    elf = bytearray(b"\x7fELF\x01\x01\x01" + bytes(9))
    elf += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, base, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    elf += struct.pack("<8I", 1, 0x100, base, base, len(code), len(code), 5, 0x10)
    return bytes(elf + bytes(0x100 - len(elf)) + code)


def test_a_ps2_dvd_iso_excludes_an_unrelated_elf_at_main_vram(tmp_path):
    """A large PS2 DVD is streamed; an independent ELF at the boot program's address isn't an overlay."""
    boot = _ps2_elf(0x00100000)
    ntgui = _ps2_elf(0x00100000)
    game_overlay = _ps2_elf(0x007C4880)

    disc = _iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_214.10;1\r\n", "SLUS_214.10": boot,
                 "NTGUI.ELF": ntgui, "GAME.ELF": game_overlay})
    path = tmp_path / "game.iso"
    path.write_bytes(disc)
    with path.open("ab") as image:
        image.truncate(64 * 1024 * 1024 + 1)  # take the streaming-disc path without a large fixture

    picked = detect.identify(path)
    assert picked.platform == "ps2"
    assert picked.filename == "game.elf"
    assert picked.info["boot"] == "SLUS_214.10"
    assert picked.data == boot
    assert [(name, addr) for name, _, addr, _ in picked.overlays] == [("game", 0x007C4880)]

