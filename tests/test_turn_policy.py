"""Turn policy uses conversation facts without transport objects."""

import pytest

from belle_agent.turn_policy import conversation_key, mid_turn_mode


@pytest.mark.parametrize(
    ("configured", "shared", "separate", "expected"),
    [
        ("steer", False, False, "steer"),
        ("steer", True, False, "queue"),
        ("steer", False, True, "queue"),
        ("cancel", True, True, "cancel"),
        ("invalid", False, False, "queue"),
    ],
)
def test_mid_turn_mode(configured, shared, separate, expected):
    assert (
        mid_turn_mode(
            configured,
            shared_session=shared,
            requires_separate_turn=separate,
        )
        == expected
    )


_PLACE = {"platform": "loopback", "workspace": "W1", "conversation": "C1", "sender": "U1", "thread": "root"}


def test_conversation_key_separates_direct_shared_and_threaded_sessions():
    assert conversation_key(**_PLACE, direct=True) == "loopback_W1_C1_U1"
    assert conversation_key(**_PLACE, direct=False) == "loopback_W1_C1_root"
    assert conversation_key(**_PLACE, direct=False, shared_session=True) == "loopback_W1_C1"
    # A direct message keys on its sender whatever the scope says about sessions.
    assert conversation_key(**_PLACE, direct=True, shared_session=True) == "loopback_W1_C1_U1"
    assert conversation_key(**{**_PLACE, "workspace": " "}, direct=False) == ""
