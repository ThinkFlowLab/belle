"""Fixtures of the conformance suite.

``kit`` is the only place a backend is named, and ``platform_kit`` the only
place a platform is. A later backend or platform adds a parameter here that
returns its own kit. See ``conformance_kit``.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import pytest
import pytest_asyncio
from conformance_kit import Kit, LoopbackKit, PlatformKit, World, worlds
from scripted_backend import ScriptedBackend

#: Checks that Belle cannot pass on Docker Agent yet. Docker Agent offers the toolsets that they need,
#: but Belle's adapter and agent YAML do not support them.
NOT_ON_DOCKER_AGENT = {
    "test_an_invented_question_pauses_the_turn_and_its_answer_resumes_it": "the user_prompt toolset",
    "test_turn_stop_withdraws_a_backend_question": "the user_prompt toolset",
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": "the user_prompt toolset",
    "test_a_turn_ends_when_the_work_it_started_has_ended_and_posts_each_answer_once": "the background_agents toolset",
}


_ACP_ASK = "hook that holds a call of Belle's tools for a person, so Belle refuses a call at a scope's tools: ask"
_ACP_MODEL = "model choice: a scope's model is not applied"
_ACP_CATALOG = "catalog of skills, subagents and models"
_ACP_FORGET = "client method for session/delete in the pinned SDK"
_ACP_RESTART = "turn that outlives Belle's connection: the plain relay ends it, and recovery reports it unknown"
#: Checks that need what the ACP backend does not have, each with what is missing.
NOT_ON_ACP = {
    "test_a_rejected_tool_leaves_no_side_effect": _ACP_ASK,
    "test_the_approval_prompt_appears_once_in_the_requesters_conversation": _ACP_ASK,
    "test_only_an_allowed_person_releases_the_call": _ACP_ASK,
    "test_a_person_the_platform_does_not_admit_answers_no_question": _ACP_ASK,
    "test_with_no_approve_rule_anyone_in_the_conversation_may_answer": _ACP_ASK,
    "test_a_question_asked_in_one_workspace_is_not_answered_from_another": _ACP_ASK,
    "test_a_stopped_turn_takes_its_held_call_with_it": _ACP_ASK,
    "test_an_invented_question_pauses_the_turn_and_its_answer_resumes_it": "question that Belle puts to a turn",
    "test_turn_stop_withdraws_a_backend_question": "question that Belle puts to a turn",
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": "question that Belle puts to a turn",
    "test_a_turn_ends_when_the_work_it_started_has_ended_and_posts_each_answer_once": "work a turn leaves running",
    "test_the_catalog_lists_what_the_backend_declares": _ACP_CATALOG,
    "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing": _ACP_CATALOG,
    "test_a_skill_required_of_one_turn_is_required_of_that_turn_only": "skill that a turn is required to use",
    "test_the_model_chosen_for_a_conversation_serves_its_turns_until_the_choice_is_removed": _ACP_MODEL,
    "test_a_scopes_model_comes_before_the_one_chosen_for_the_conversation": _ACP_MODEL,
    "test_a_model_scoped_to_one_sender_serves_that_senders_turns_only": _ACP_MODEL,
    "test_content_the_backend_hands_to_its_model_is_returned": "measured way to hand tool content beside text to the model",
    "test_content_the_backend_does_not_hand_to_its_model_is_refused_with_the_types_it_takes": (
        "measured way to hand tool content beside text to the model"
    ),
    "test_expiry_keeps_attached_session": _ACP_FORGET,
    "test_idle_attached_session_expiry": _ACP_FORGET,
    "test_a_session_gone_from_the_harness_or_the_store_is_not_continued": _ACP_FORGET,
    "test_chat_turn_of_a_stopped_host": _ACP_RESTART,
    "test_session_run_of_a_stopped_host": _ACP_RESTART,
    "test_chat_turn_outlives_the_host_that_started_it": _ACP_RESTART,
}


@pytest.fixture(
    params=[
        "scripted",
        pytest.param("opencode", marks=pytest.mark.adapter),
        pytest.param("docker_agent", marks=pytest.mark.docker_agent_server),
        "acp",
    ]
)
def kit(request: pytest.FixtureRequest) -> Iterator[Kit]:
    if request.param == "scripted":
        yield ScriptedBackend()
        return
    if request.param == "acp":
        pytest.importorskip("acp", reason="the ACP kit needs the acp extra")
        if request.node.originalname in NOT_ON_ACP:
            pytest.skip(f"the ACP backend has no {NOT_ON_ACP[request.node.originalname]}")
        from acp_conformance_kit import AcpKit as HarnessKit
    elif request.param == "docker_agent":
        if request.node.originalname in NOT_ON_DOCKER_AGENT:
            pytest.skip(f"Belle does not support {NOT_ON_DOCKER_AGENT[request.node.originalname]} of Docker Agent yet")
        from docker_agent_kit import DockerAgentKit as HarnessKit
    else:
        from opencode_kit import OpenCodeKit as HarnessKit

    with HarnessKit.scripted() as harness:
        yield harness


#: The checks that hold on the loopback platform and cannot hold on Slack, each with the difference behind it.
#: A check missing from this table that fails on Slack marks behaviour that leaked into the host.
ONLY_ON_LOOPBACK = {
    "test_a_stop_for_a_turn_that_is_not_running_stops_nothing": (
        "Slack's stop button names a thread and not a message, so a stop cannot be aimed at a message that"
        " waits in the thread of a running turn: it stops the turn that runs there"
    ),
    "test_the_host_names_each_second_brain_setting_that_does_nothing": (
        "Slack says which conversations are public, so the `public` entry of second_brain.ingest.channels"
        " selects something there and the host has nothing to warn about"
    ),
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": (
        "the Slack kit reads its log from web API calls, which carry no workspace: each client is one workspace's."
        " tests/test_slack_workspaces.py checks which client was called"
    ),
    "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing": (
        "the check hands the host a direct message with the loopback chat type dm, and its scope names dm."
        " Slack has no chat type dm, so Belle does not start with that scope"
    ),
}

#: Where two Slack workspaces differ from one: the log names the workspace of the app that was asked.
NOT_ON_SLACK_WORKSPACES = {
    **{
        name: reason
        for name, reason in ONLY_ON_LOOPBACK.items()
        if name != "test_every_action_the_host_asks_for_names_the_workspace_of_its_message"
    },
    "test_a_question_asked_in_one_workspace_is_not_answered_from_another": (
        "a button is posted by one workspace's app, and only that app receives its click, as the click of its own"
        " workspace. The other app was shown no question, so no click can come from the other workspace"
    ),
}

#: Why a check cannot hold on the Mattermost platform. Most name a part the connector does not have yet.
_IDS_ARE_NO_CHANNEL_IDS = (
    "the check posts into conversation C2, which is no Mattermost channel id: the posting tool refuses chat_id C2"
    " as malformed, so the post that a person agrees to is never made"
)
_ONE_SERVER = (
    "a channel id names one channel of the server, and a click names no workspace: the question's own record"
    " gives the workspace, so no click can come from another workspace"
)
_LOOPBACK_WORDS = (
    "the check names a direct message's place in the loopback words, workspace W1 and chat type dm, where the"
    " connector writes workspace direct and chat type D"
)
_NO_PIN = (
    "the server reports a pin as an edit of the post that names nobody as the one who pinned it, so the connector"
    " hands over no pin event"
)
_NO_VISIT = "Mattermost has no App Home, so the connector hands over no visit"
_SEQ_IS_THE_DELIVERY_ID = (
    "a Mattermost event's delivery id is its connection's id and its sequence number, so the kit cannot give it"
    " the delivery id that the check looks for in the log"
)
_ONE_POST_ONE_ID = (
    "a Mattermost post has one id, which is also its event id, so one request cannot come again under"
    " another message id"
)
_LOG_HAS_NO_WORKSPACE = (
    "the Mattermost kit reads its log from REST calls, which carry no workspace: one connector serves one server"
)
_PUBLIC_KNOWN = (
    "Mattermost says which channels are public, so the `public` entry of second_brain.ingest.channels selects"
    " something there and the host has nothing to warn about"
)
#: The checks that cannot hold on the Mattermost platform, each with the difference behind it.
#: A check missing from this table that fails on Mattermost marks behaviour that leaked into the host.
NOT_ON_MATTERMOST = {
    "test_a_call_a_person_must_agree_to_is_asked_about_and_made_once_when_they_agree": _IDS_ARE_NO_CHANNEL_IDS,
    "test_a_held_partial_post_reports_the_parts_already_sent": _IDS_ARE_NO_CHANNEL_IDS,
    "test_a_call_nobody_can_be_asked_about_is_refused": _IDS_ARE_NO_CHANNEL_IDS,
    "test_a_conversation_keeps_a_bounded_number_of_events_and_says_how_many_it_dropped": _NO_VISIT,
    "test_a_pin_that_names_no_message_is_lost_and_not_kept": _NO_PIN,
    "test_a_question_asked_in_one_workspace_is_not_answered_from_another": _ONE_SERVER,
    "test_a_refusal_sends_nothing_and_the_next_turn_is_told": _IDS_ARE_NO_CHANNEL_IDS,
    "test_a_request_that_comes_twice_runs_once_and_is_answered_once": _ONE_POST_ONE_ID,
    "test_a_stopped_turn_takes_its_held_call_with_it": _IDS_ARE_NO_CHANNEL_IDS,
    "test_a_visit_is_told_to_the_visitor_s_own_direct_turn": _NO_VISIT,
    "test_an_answer_that_comes_too_late_sends_nothing": _IDS_ARE_NO_CHANNEL_IDS,
    "test_an_event_that_cannot_wait_is_lost_and_told_to_nobody": _SEQ_IS_THE_DELIVERY_ID,
    "test_an_event_turn_speaks_where_its_event_happened": _NO_PIN,
    "test_events_lost_when_the_host_stops_are_each_named": _SEQ_IS_THE_DELIVERY_ID,
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": _LOG_HAS_NO_WORKSPACE,
    "test_the_host_names_each_second_brain_setting_that_does_nothing": _PUBLIC_KNOWN,
    "test_the_model_chosen_for_a_conversation_serves_its_turns_until_the_choice_is_removed": _LOOPBACK_WORDS,
    "test_the_same_call_made_twice_is_asked_about_once": _IDS_ARE_NO_CHANNEL_IDS,
    "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing": _LOOPBACK_WORDS,
    "test_what_was_decided_is_told_to_the_next_turn_after_a_restart": _IDS_ARE_NO_CHANNEL_IDS,
    "test_who_may_agree_follows_the_clicks_rule": _IDS_ARE_NO_CHANNEL_IDS,
    "test_with_no_approve_rule_anyone_in_the_conversation_may_agree": _IDS_ARE_NO_CHANNEL_IDS,
}
_WORKSPACE_NAMES_ITS_SERVER = (
    "the check names workspace W1 to the host itself, in a record or in a question about what runs, and not through"
    " the connector. With several servers a workspace starts with its server's name, as one:W1, so W1 names no"
    " server: nothing runs or waits there, and nothing is sent there"
)
#: Where two servers differ from one: workspaces are servers, so the log names them, and a click reaches one server.
NOT_ON_MATTERMOST_SERVERS = {
    **{name: reason for name, reason in NOT_ON_MATTERMOST.items() if reason is not _LOG_HAS_NO_WORKSPACE},
    **dict.fromkeys(
        (
            "test_what_runs_and_what_waits_is_read_per_conversation",
            "test_a_waiting_message_removed_by_its_sender_never_runs",
            "test_another_persons_waiting_message_is_removed_only_by_whoever_a_rule_names",
            "test_with_no_rule_only_its_sender_removes_a_waiting_message",
            "test_a_command_surface_reads_the_rule_a_stop_is_held_to",
            "test_a_skill_required_of_one_turn_is_required_of_that_turn_only",
            "test_a_side_conversation_shares_no_session_with_the_one_it_sits_in",
            "test_a_rule_does_not_let_somebody_outside_the_allow_list_remove_or_stop",
            "test_a_turn_that_requires_an_unavailable_skill_is_refused",
            "test_scheduled_final_starts_new_thread_and_records_its_post",
            "test_scheduled_sentinel_is_silent_even_when_reply_is_normally_required",
        ),
        _WORKSPACE_NAMES_ITS_SERVER,
    ),
    "test_a_question_asked_in_one_workspace_is_not_answered_from_another": (
        "a button posted on one server sends its click to the listener of that server only. A click that another"
        " server sends carries a secret of another listener, so it is taken as a button of an earlier start"
    ),
}


_NO_MATRIX_VISIT = "Matrix has no App Home, so the connector hands over no visit"
_C2_IS_NO_ROOM_ID = (
    "the check posts into conversation C2, which is no Matrix room id: the posting tool refuses chat_id C2 as"
    " malformed, so the post is never made"
)
_ONE_ACCOUNT = (
    "the check names workspace W1 to the host itself. A Matrix account is one workspace, `default`, so nothing"
    " runs, waits or is chosen under W1"
)
#: Why a check cannot hold on the Matrix platform. Each names a difference of Matrix: every part is built.
#: A check missing from this table that fails on Matrix marks behaviour that leaked into the host.
NOT_ON_MATRIX: dict[str, str] = {
    **dict.fromkeys(
        (
            "test_a_conversation_keeps_a_bounded_number_of_events_and_says_how_many_it_dropped",
            "test_a_visit_is_told_to_the_visitor_s_own_direct_turn",
        ),
        _NO_MATRIX_VISIT,
    ),
    "test_a_call_nobody_can_be_asked_about_is_refused": _C2_IS_NO_ROOM_ID,
    **dict.fromkeys(
        (
            "test_a_call_a_person_must_agree_to_is_asked_about_and_made_once_when_they_agree",
            "test_a_held_partial_post_reports_the_parts_already_sent",
            "test_a_refusal_sends_nothing_and_the_next_turn_is_told",
            "test_a_stopped_turn_takes_its_held_call_with_it",
            "test_an_answer_that_comes_too_late_sends_nothing",
            "test_the_same_call_made_twice_is_asked_about_once",
            "test_what_was_decided_is_told_to_the_next_turn_after_a_restart",
            "test_who_may_agree_follows_the_clicks_rule",
            "test_with_no_approve_rule_anyone_in_the_conversation_may_agree",
        ),
        _C2_IS_NO_ROOM_ID,
    ),
    "test_a_question_asked_in_one_workspace_is_not_answered_from_another": (
        "a Matrix account is one workspace, `default`, so a vote always comes from the workspace of its poll"
    ),
    **dict.fromkeys(
        (
            "test_a_command_surface_reads_the_rule_a_stop_is_held_to",
            "test_a_rule_does_not_let_somebody_outside_the_allow_list_remove_or_stop",
            "test_a_skill_required_of_one_turn_is_required_of_that_turn_only",
            "test_a_waiting_message_removed_by_its_sender_never_runs",
            "test_another_persons_waiting_message_is_removed_only_by_whoever_a_rule_names",
            "test_the_model_chosen_for_a_conversation_serves_its_turns_until_the_choice_is_removed",
            "test_the_same_ids_in_two_workspaces_share_no_session_no_queue_and_no_seen_message",
            "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing",
            "test_what_runs_and_what_waits_is_read_per_conversation",
            "test_with_no_rule_only_its_sender_removes_a_waiting_message",
        ),
        _ONE_ACCOUNT,
    ),
    "test_an_event_turn_speaks_where_its_event_happened": (
        "the check pins message 1710000001.000100, which is no Matrix event id: the posting tool refuses it as"
        " reply_to_message_id, so the answer in the thread of the pinned message is never made"
    ),
    "test_an_event_that_cannot_wait_is_lost_and_told_to_nobody": _ONE_ACCOUNT,
    "test_a_stop_for_a_turn_that_is_not_running_stops_nothing": (
        "on Matrix one reaction asks for both acts (D18): a stop key on a message that waits for its turn removes"
        " that message, so a stop aimed at a waiting message does not leave it waiting"
    ),
    "test_a_request_that_comes_twice_runs_once_and_is_answered_once": (
        "a Matrix message has one id, its event id, so one request cannot come again under another message id"
    ),
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": (
        "the Matrix kit reads its log from the helper's requests, which carry no workspace: one helper serves one account"
    ),
    "test_the_host_names_each_second_brain_setting_that_does_nothing": (
        "Matrix says which rooms are public, so the `public` entry of second_brain.ingest.channels selects something"
        " there and the host has nothing to warn about"
    ),
}


_HANDED_IN_LOOPBACK_WORDS = (
    "the check hands the host itself a direct message in the loopback words: chat type dm in conversation D-U1."
    " The Matrix connector writes chat type direct, so a scope for direct does not match the record, and it reads"
    " D-U1 as a room of three people when the host acts on the record, so the kit's next message there is no"
    " direct message"
)
#: Where two accounts differ from one: the accounts are named W1 and W2, so the checks that name a workspace hold.
NOT_ON_MATRIX_ACCOUNTS = {
    **{
        name: reason
        for name, reason in NOT_ON_MATRIX.items()
        if reason is not _ONE_ACCOUNT
        and name not in ("test_a_question_asked_in_one_workspace_is_not_answered_from_another",
                         "test_every_action_the_host_asks_for_names_the_workspace_of_its_message")
    },
    **dict.fromkeys(
        (
            "test_a_skill_required_of_one_turn_is_required_of_that_turn_only",
            "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing",
        ),
        _HANDED_IN_LOOPBACK_WORDS,
    ),
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": (
        "the bot replies to an answered poll with who answered (D16), so a note of the answer's workspace comes"
        " between the question and the post"
    ),
}


#: Why a check cannot hold on the Discord platform, each with the Discord difference or the phase that brings it.
#: A check missing from this table that fails on Discord marks behaviour that leaked into the host.
_DISCORD_SYNTHETIC_CHANNEL_IDS = (
    "the check's C2 is not a Discord channel ID, so the posting tool refuses it before asking or posting"
)
_DISCORD_D6_TOOLS = "the Discord toolkit has only post_message until the other tools come (note 14, phase D6)"
_DISCORD_MEMBER = (
    "the Discord member queue, loss, and skill-policy fixtures have not been verified with system-channel routing"
)
_DISCORD_NO_PIN = "Discord reports a pin with CHANNEL_PINS_UPDATE, which names neither the person nor the message"
_DISCORD_NO_VISIT = "Discord has no App Home, so the connector hands over no visit"
NOT_ON_DISCORD: dict[str, str] = {
    **dict.fromkeys(
        (
            "test_a_call_a_person_must_agree_to_is_asked_about_and_made_once_when_they_agree",
            "test_a_held_partial_post_reports_the_parts_already_sent",
            "test_a_call_nobody_can_be_asked_about_is_refused",
            "test_a_refusal_sends_nothing_and_the_next_turn_is_told",
            "test_a_stopped_turn_takes_its_held_call_with_it",
            "test_an_answer_that_comes_too_late_sends_nothing",
            "test_the_same_call_made_twice_is_asked_about_once",
            "test_what_was_decided_is_told_to_the_next_turn_after_a_restart",
            "test_who_may_agree_follows_the_clicks_rule",
            "test_with_no_approve_rule_anyone_in_the_conversation_may_agree",
        ),
        _DISCORD_SYNTHETIC_CHANNEL_IDS,
    ),
    **dict.fromkeys(
        (
            "test_a_reaction_lands_on_the_source_message_and_posts_nothing",
            "test_a_rejected_tool_leaves_no_side_effect",
            "test_only_an_allowed_person_releases_the_call",
            "test_a_person_the_platform_does_not_admit_answers_no_question",
            "test_with_no_approve_rule_anyone_in_the_conversation_may_answer",
            "test_a_question_asked_in_one_workspace_is_not_answered_from_another",
            "test_content_the_backend_does_not_hand_to_its_model_is_refused_with_the_types_it_takes",
            "test_content_the_backend_hands_to_its_model_is_returned",
        ),
        _DISCORD_D6_TOOLS,
    ),
    **dict.fromkeys(
        (
            "test_a_message_waits_for_the_turn_of_an_event_whatever_mid_turn_says",
            "test_a_turn_an_event_starts_is_not_put_to_the_decision_model",
            "test_an_event_that_cannot_wait_is_lost_and_told_to_nobody",
            "test_an_event_that_finds_its_conversation_busy_waits_behind_the_messages",
            "test_an_event_that_waited_too_long_is_lost_and_told_to_nobody",
            "test_an_event_turn_that_requires_an_unavailable_skill_is_refused",
        ),
        _DISCORD_MEMBER,
    ),
    "test_every_action_the_host_asks_for_names_the_workspace_of_its_message": (
        "a Discord channel belongs to one guild, so one channel cannot carry a message in two workspaces"
    ),
    "test_an_event_turn_speaks_where_its_event_happened": _DISCORD_NO_PIN,
    "test_a_pin_that_names_no_message_is_lost_and_not_kept": _DISCORD_NO_PIN,
    "test_events_lost_when_the_host_stops_are_each_named": f"{_DISCORD_MEMBER}; and {_DISCORD_NO_PIN}",
    "test_a_conversation_keeps_a_bounded_number_of_events_and_says_how_many_it_dropped": _DISCORD_NO_VISIT,
    "test_a_visit_is_told_to_the_visitor_s_own_direct_turn": _DISCORD_NO_VISIT,
    "test_a_request_that_comes_twice_runs_once_and_is_answered_once": (
        "a Discord message has one id, which is also its delivery id, so one request cannot come again under"
        " another message id"
    ),
    "test_the_model_chosen_for_a_conversation_serves_its_turns_until_the_choice_is_removed": (
        "the check names a direct message's place in the loopback words, workspace W1 and chat type dm, where the"
        " connector writes workspace direct"
    ),
}


_WORKSPACE_NAMES_ITS_BOT = (
    "the check names workspace W1 to the host itself, in a record or in a question about what runs, and not through"
    " the connector. With several bots a workspace starts with its bot's name, as east:W1, so W1 names no bot:"
    " nothing runs or waits there, and nothing is sent there"
)
#: Where two Discord bots differ from one: each bot has its own guilds, and the log names the workspace of the bot.
NOT_ON_DISCORD_BOTS = {
    **{
        name: reason
        for name, reason in NOT_ON_DISCORD.items()
        if name != "test_every_action_the_host_asks_for_names_the_workspace_of_its_message"
    },
    **dict.fromkeys(
        (
            "test_what_runs_and_what_waits_is_read_per_conversation",
            "test_a_waiting_message_removed_by_its_sender_never_runs",
            "test_another_persons_waiting_message_is_removed_only_by_whoever_a_rule_names",
            "test_with_no_rule_only_its_sender_removes_a_waiting_message",
            "test_a_command_surface_reads_the_rule_a_stop_is_held_to",
            "test_a_skill_required_of_one_turn_is_required_of_that_turn_only",
            "test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing",
            "test_a_side_conversation_shares_no_session_with_the_one_it_sits_in",
            "test_a_rule_does_not_let_somebody_outside_the_allow_list_remove_or_stop",
            "test_a_turn_that_requires_an_unavailable_skill_is_refused",
            "test_scheduled_final_starts_new_thread_and_records_its_post",
            "test_scheduled_sentinel_is_silent_even_when_reply_is_normally_required",
        ),
        _WORKSPACE_NAMES_ITS_BOT,
    ),
}


@pytest.fixture(
    params=[
        "loopback", "slack", "slack_workspaces", "mattermost", "mattermost_servers", "matrix", "matrix_accounts", "discord",
        "discord_bots",
    ]
)
def platform_kit(request: pytest.FixtureRequest, monkeypatch) -> PlatformKit:
    """The platform a host is built on. The loopback platform comes first: every check is written for it."""
    if request.param == "loopback":
        return LoopbackKit()
    if request.param == "matrix":
        if request.node.originalname in NOT_ON_MATRIX:
            pytest.skip(f"a Matrix difference: {NOT_ON_MATRIX[request.node.originalname]}")
        from matrix_kit import MatrixKit

        return MatrixKit(monkeypatch)
    if request.param == "matrix_accounts":
        if request.node.originalname in NOT_ON_MATRIX_ACCOUNTS:
            pytest.skip(f"a Matrix difference: {NOT_ON_MATRIX_ACCOUNTS[request.node.originalname]}")
        from matrix_kit import MatrixAccountsKit

        return MatrixAccountsKit(monkeypatch)
    if request.param == "mattermost":
        if request.node.originalname in NOT_ON_MATTERMOST:
            pytest.skip(f"a Mattermost difference: {NOT_ON_MATTERMOST[request.node.originalname]}")
        from mattermost_kit import MattermostKit

        return MattermostKit(monkeypatch)
    if request.param == "mattermost_servers":
        if request.node.originalname in NOT_ON_MATTERMOST_SERVERS:
            pytest.skip(f"a Mattermost difference: {NOT_ON_MATTERMOST_SERVERS[request.node.originalname]}")
        from mattermost_kit import MattermostServersKit

        return MattermostServersKit(monkeypatch)
    if request.param == "discord_bots":
        if request.node.originalname in NOT_ON_DISCORD_BOTS:
            pytest.skip(f"a Discord difference: {NOT_ON_DISCORD_BOTS[request.node.originalname]}")
        from discord_kit import DiscordBotsKit

        return DiscordBotsKit(monkeypatch)
    if request.param == "discord":
        if request.node.originalname in NOT_ON_DISCORD:
            pytest.skip(f"a Discord difference: {NOT_ON_DISCORD[request.node.originalname]}")
        from discord_kit import DiscordKit

        return DiscordKit(monkeypatch)
    if request.param == "slack_workspaces":
        if request.node.originalname in NOT_ON_SLACK_WORKSPACES:
            pytest.skip(f"a Slack difference: {NOT_ON_SLACK_WORKSPACES[request.node.originalname]}")
        from slack_kit import SlackWorkspacesKit

        return SlackWorkspacesKit(monkeypatch)
    if request.node.originalname in ONLY_ON_LOOPBACK:
        pytest.skip(f"a Slack difference: {ONLY_ON_LOOPBACK[request.node.originalname]}")
    from slack_kit import SlackKit

    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def build_world(kit: Kit, platform_kit: PlatformKit, tmp_path) -> AsyncIterator[Callable[..., Awaitable[World]]]:
    """Return a builder that starts one host from a Belle configuration file."""
    async with worlds(kit, tmp_path, platform_kit) as build:
        yield build


@pytest_asyncio.fixture
async def world(build_world) -> World:
    """A host with no scopes: direct messages and mentions are answered."""
    return await build_world()
