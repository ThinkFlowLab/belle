import pytest

from belle_agent.turn_policy import conversation_key


@pytest.mark.parametrize(("direct", "shared"), [(True, False), (False, False), (False, True)])
def test_conversation_key_is_the_slack_session_id_for_slack(direct, shared):
    routing = pytest.importorskip("belle_agent.integrations.jiuwenswarm.slack_routing")
    assert conversation_key(
        platform="slack", workspace="T1", conversation="C1", sender="U1", thread="1.2",
        direct=direct, shared_session=shared,
    ) == routing.slack_session_id(
        team_id="T1", channel_id="C1", user_id="U1", root_thread_ts="1.2", is_dm=direct, channel_wide=shared,
    )
