"""What a helper does when its agent says it can't go on: stop for good, not start session after session."""

from decompnet.app.agents import no_credit


def test_a_used_up_subscription_quota_stops_the_helper():
    why = no_credit("You've hit your session limit · resets 6pm (America/Cordoba)")
    assert why and "quota" in why and "resets 6pm" in why


def test_an_empty_account_stops_the_helper_and_ordinary_failures_do_not():
    assert "top it up" in no_credit("Error: Insufficient Balance")
    assert no_credit("Error: 429 Too Many Requests") is None  # throttled: waits and tries again
    assert no_credit("exited with code 1: compile failed") is None
