"""Cutting every claim of a project, and only that project's."""

import pytest

from decompnet.network import Network, NetworkError


def test_clearing_claims_touches_only_that_project(tmp_path):
    net = Network(tmp_path / "decomp.db")
    with net.store.tx() as db:
        for pid, slug in ((1, "one"), (2, "two")):
            db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(?,?,?,?,?)",
                       (pid, slug, slug, str(tmp_path), "{}"))
        for fid, pid in ((1, 1), (2, 1), (3, 2)):
            db.execute("INSERT INTO functions(id, project_id, name) VALUES(?,?,?)", (fid, pid, f"f{fid}"))
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, 7, 0, 1e12))

    assert net.clear_claims("one") == 2
    assert [r["function_id"] for r in net.store.all("SELECT function_id FROM claims")] == [3]
    assert net.clear_claims("one") == 0  # nothing left to cut
    with pytest.raises(NetworkError):
        net.clear_claims("missing")
