"""A helper launched for the ★ Next in line functions only: it gets those, nothing else, and is told when they're done."""

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


@pytest.fixture
def net(tmp_path):
    net = RomNetwork(tmp_path / "decomp.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"priority": ["star_b", "star_a"]})))
        for name in ("easy", "star_a", "star_b"):  # `easy` would be everybody's first pick
            db.execute("INSERT INTO functions(project_id, name, size, n_insns) VALUES(1,?,?,?)",
                       (name, 4, 1 if name == "easy" else 50))
    return net


def test_a_starred_only_helper_takes_the_stars_in_order_and_nothing_else(net):
    helper, _ = net.local_agent("helper")
    net.starred_only.add(helper.id)

    with pytest.raises(NetworkError, match="isn't marked"):
        net.claim_row(helper, "game", "easy")  # by name, but not starred

    first = net.claim_row(helper, "game")
    assert first["name"] == "star_b"  # the pin list's order, not the easiest first
    net.release(helper, "star_b", "", "game")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='star_b'")
    assert net.claim_row(helper, "game")["name"] == "star_a"
    net.release(helper, "star_a", "", "game")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='star_a'")

    with pytest.raises(NetworkError, match="no ★ Next in line function is free"):
        net.claim_row(helper, "game")  # all starred are done: it doesn't fall back to `easy`


def test_other_helpers_are_not_limited_to_the_stars(net):
    other, _ = net.local_agent("other")
    assert net.claim_row(other, "game", "easy")["name"] == "easy"


def test_the_session_knows_how_many_stars_are_left_and_how_many_are_free_to_take(net, tmp_path):
    from decompnet.app.agents import Manager

    manager = Manager(net, tmp_path, "http://127.0.0.1:0/mcp")
    helper, _ = net.local_agent("helper")
    other, _ = net.local_agent("other")
    assert manager._starred_left("game", helper.id) == (2, 2)

    net.claim_row(other, "game", "star_b")  # somebody else is on one of them
    assert manager._starred_left("game", helper.id) == (2, 1)

    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name IN ('star_a','star_b')")
    assert manager._starred_left("game", helper.id) == (0, 0)  # done: the session ends
