"""What a helper does when its agent says it can't go on: stop for good, not start session after session."""

from decompnet.app.agents import no_credit


def test_a_used_up_subscription_quota_stops_the_helper():
    why = no_credit("You've hit your session limit · resets 6pm (America/Cordoba)")
    assert why and "quota" in why and "resets 6pm" in why


def test_an_empty_account_stops_the_helper_and_ordinary_failures_do_not():
    assert "top it up" in no_credit("Error: Insufficient Balance")
    assert no_credit("Error: 429 Too Many Requests") is None  # throttled: waits and tries again
    assert no_credit("exited with code 1: compile failed") is None


def test_opencode_gets_a_log_level_every_version_accepts(tmp_path):
    """OpenCode 2 stops with "Invalid value for flag --log-level" on "ERROR"; it and 1.x both take "error"."""
    from decompnet.app.agents import Manager, Session

    manager = Manager(None, tmp_path, "http://127.0.0.1:0/mcp")
    manager.prompt = lambda s: "prompt"
    session = Session("opencode-1", "opencode", "opencode #1", "game", "opencode-1", {})
    import decompnet.app.agents as agents
    real = agents._which
    agents._which = lambda name: "/usr/bin/opencode"
    try:
        cmd, _, _ = manager._command(session, "token", tmp_path)
    finally:
        agents._which = real
    assert cmd[cmd.index("--log-level") + 1] == "error"
