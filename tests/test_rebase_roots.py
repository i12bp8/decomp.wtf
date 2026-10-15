"""A workspace opened from another place than the one that made it (Docker's /home/decomp, then the Mac folder)."""

from decompnet.app.engine import RomNetwork


def add(net, slug, root):
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(slug, name, root, config) VALUES(?,?,?,?)", (slug, slug, str(root), "{}"))


def root_of(net, slug):
    return net.store.one("SELECT root FROM projects WHERE slug=?", slug)["root"]


def test_a_project_follows_its_folder_between_places(tmp_path):
    workspace, db = tmp_path / "Decomp", tmp_path / "decomp.db"
    (workspace / "game").mkdir(parents=True)
    (workspace / "kept").mkdir()
    (tmp_path / "elsewhere").mkdir()
    net = RomNetwork(db, workspace)
    gone = tmp_path / "home" / "decomp" / "Decomp"  # where the project was made: doesn't exist here
    add(net, "game", gone / "game")
    add(net, "kept", workspace / "kept")
    add(net, "elsewhere", tmp_path / "elsewhere")  # exists, just not in the workspace: left alone
    add(net, "nofolder", gone / "nofolder")  # no folder in this workspace either: nothing to move to

    net = RomNetwork(db, workspace)  # the app starts again, now from this place

    assert root_of(net, "game") == str(workspace / "game")
    assert root_of(net, "kept") == str(workspace / "kept")
    assert root_of(net, "elsewhere") == str(tmp_path / "elsewhere")
    assert root_of(net, "nofolder") == str(gone / "nofolder")
