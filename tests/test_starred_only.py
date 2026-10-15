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
    net.starred_only[helper.id] = 0.0

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

    with pytest.raises(NetworkError, match="no ★ Next in line function is left for you"):
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


def test_a_star_the_helper_gave_back_is_not_handed_to_it_again(net, tmp_path):
    """It was found to be data, not code: the next claim moves on, and when nothing else is left the session ends
    (without this the same star came back every time and a new paid session started for it)."""
    import time

    from decompnet.app.agents import Manager

    manager = Manager(net, tmp_path, "http://127.0.0.1:0/mcp")
    helper, _ = net.local_agent("helper")
    began = time.time() - 1
    net.starred_only[helper.id] = began

    assert net.claim_row(helper, "game")["name"] == "star_b"
    net.release(helper, "star_b", "that's data", "game")
    assert net.claim_row(helper, "game")["name"] == "star_a"  # not star_b again
    net.release(helper, "star_a", "data too", "game")
    assert manager._starred_left("game", helper.id, began) == (0, 0)  # nothing worth trying: the session ends
    with pytest.raises(NetworkError, match="no ★ Next in line function is left for you"):
        net.claim_row(helper, "game")
    assert manager._starred_left("game", helper.id, time.time() + 5) == (2, 2)  # a later session tries them anew


def test_a_match_takes_the_function_off_next_in_line(net):
    agent, _ = net.local_agent("helper")
    fn = net.claim_row(agent, "game", "star_a")
    net._record(fn, agent, 0, "int star_a(void) { return 0; }", 1, matched=True)
    assert net.cfg(net._project("game"))["priority"] == ["star_b"]


def test_the_list_never_shows_a_function_that_is_already_done(net):
    with net.store.tx() as db:  # matched some other way (a twin, a rescan...), still in the saved list
        db.execute("UPDATE functions SET status='matched' WHERE name='star_b'")
    assert net._open_priority(net._project("game"), ["star_b", "star_a"]) == ["star_a"]
