"""Regressions for member and pin events that wait for their own turns."""

import asyncio

import pytest
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from slack_fakes import SlackPeople

from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.integrations.jiuwenswarm import slack_inbound
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
    SlackChannelOverride,
)
from belle_agent.platforms.slack import command_ui

pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


def _channel(
    *, family: str = "member", reply: str = "optional",
    mid_turn: str | None = None,
):
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=["C1"],
        conversation_overrides={
            "C1": SlackChannelOverride(
                events={family: "turn"}, reply=reply, session="channel",
                mid_turn=mid_turn,
            )
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._client = SlackPeople()
    channel._running = True
    channel._identity.bot_user_id = "U0BOT"
    requests = []
    channel.on_message(requests.append)
    return channel, requests


def _member(user: str, event_id: str):
    return (
        {
            "type": "member_joined_channel",
            "user": user,
            "channel": "C1",
            "channel_type": "C",
            "event_ts": f"1710000300.{int(event_id[2:]):06d}",
        },
        {"event_id": event_id, "team_id": "T1"},
    )


def _pin(user: str, event_id: str):
    return (
        {
            "type": "pin_added",
            "user": user,
            "channel_id": "C1",
            "item": {
                "type": "message",
                "channel": "C1",
                "message": {"ts": "1710000000.000100"},
            },
            "event_ts": f"1710000200.{int(event_id[2:]):06d}",
        },
        {"event_id": event_id, "team_id": "T1"},
    )


def _message(user: str, event_id: str):
    return (
        {
            "type": "message",
            "channel": "C1",
            "channel_type": "channel",
            "user": user,
            "text": "<@U0BOT> hello",
            "ts": f"1710000400.{int(event_id[2:]):06d}",
        },
        {"event_id": event_id, "team_id": "T1"},
    )


async def _finish(channel: SlackChannel, request) -> None:
    channel._forget_turn_initiator(request.session_id, request.id)
    await channel._drain_queued_messages(request.session_id)


def _status(channel: SlackChannel):
    return channel._status_facts(
        command_ui.Invocation(
            team_id="T1", user_id="U0OP", channel_id="C1", text=""
        ),
        agent_answered=False,
    )


@pytest.mark.asyncio
async def test_status_reports_that_messages_wait_for_an_event_turn() -> None:
    channel, _ = _channel(mid_turn="cancel")
    await channel._route_inbound_event(*_member("U0A", "Ev1"))

    facts = _status(channel)
    text = slack_commands.status_text(facts, "/jiuwen", bot_name="JiuwenSwarm")

    assert facts.event_running
    assert "A message sent during an event turn waits until it ends" in text
    assert "stops the current turn" not in text


@pytest.mark.asyncio
async def test_two_joins_do_not_overlap_during_deduplication(monkeypatch) -> None:
    channel, requests = _channel()
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_first(key: str) -> bool:
        if key.endswith("Ev1"):
            entered.set()
            await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_first)
    first = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        second = await channel._route_inbound_event(*_member("U0B", "Ev2"))
        assert second.startswith("held:member")
        assert [request.user_id for request in requests] == []
    finally:
        release.set()
    assert (await first).startswith("dispatched:event:member")
    assert [request.user_id for request in requests] == ["U0A"]
    assert requests[0].is_stream is True
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0A", "U0B"]
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_busy_joins_keep_arrival_order_during_deduplication(monkeypatch) -> None:
    channel, requests = _channel()
    await channel._route_inbound_event(*_member("U0FIRST", "Ev1"))
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_second(key: str) -> bool:
        if key.endswith("Ev2"):
            entered.set()
            await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_second)
    second = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev2")))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert (await channel._route_inbound_event(*_member("U0B", "Ev3"))).startswith(
            "held:member"
        )
        await _finish(channel, requests[0])
        assert [request.user_id for request in requests] == ["U0FIRST"]
    finally:
        release.set()
    assert (await second).startswith("held:member")
    assert [request.user_id for request in requests] == ["U0FIRST", "U0A"]
    await _finish(channel, requests[1])
    assert [request.user_id for request in requests] == ["U0FIRST", "U0A", "U0B"]


@pytest.mark.asyncio
async def test_later_join_cannot_reserve_an_idle_session_first(monkeypatch) -> None:
    channel, requests = _channel()
    await channel._route_inbound_event(*_member("U0FIRST", "Ev1"))
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_second(key: str) -> bool:
        if key.endswith("Ev2"):
            entered.set()
            await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_second)
    second = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev2")))
    await asyncio.wait_for(entered.wait(), 1)
    await _finish(channel, requests[0])
    try:
        assert (await channel._route_inbound_event(*_member("U0B", "Ev3"))).startswith(
            "held:member"
        )
        assert [request.user_id for request in requests] == ["U0FIRST"]
    finally:
        release.set()
    await second
    assert [request.user_id for request in requests] == ["U0FIRST", "U0A"]
    await _finish(channel, requests[1])
    assert [request.user_id for request in requests] == ["U0FIRST", "U0A", "U0B"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit", ["_MAX_PENDING_EVENTS_PER_SESSION", "_MAX_PENDING_EVENTS"]
)
async def test_stalled_admissions_obey_pending_cap(monkeypatch, limit: str) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(slack_inbound, limit, 2)
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event
    waiting = 0

    async def wait_for_event(key: str) -> bool:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            entered.set()
        await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_event)
    first = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    second = asyncio.create_task(channel._route_inbound_event(*_member("U0B", "Ev2")))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert _status(channel).pending_events == 2
        result = await asyncio.wait_for(
            channel._route_inbound_event(*_member("U0C", "Ev3")), 1
        )
        assert result.startswith(
            "dropped:pending-member-full"
        )
        assert (_status(channel).pending_events, _status(channel).lost_events) == (2, 1)
        assert any(
            all(part in line for part in ("Ev3", "U0C", "C1", "admission-full"))
            for line in warnings
        )
    finally:
        release.set()
    await asyncio.gather(first, second)
    assert [request.user_id for request in requests] == ["U0A"]
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0A", "U0B"]
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_admission_that_expires_during_deduplication_is_lost(monkeypatch) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_event(key: str) -> bool:
        entered.set()
        await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_event)
    task = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    entry = next(iter(channel._admitting_events["slack_T1_C1"].values()))
    entry.queued_at -= slack_inbound._PENDING_EVENT_TIMEOUT_SECONDS + 1
    release.set()

    assert (await task).startswith("dropped:member-expired")
    assert requests == []
    assert _status(channel).lost_events == 1
    assert any("Ev1" in line and "expired-during-admission" in line for line in warnings)


@pytest.mark.asyncio
async def test_join_then_message_preserves_two_actors_and_turn_order() -> None:
    channel, requests = _channel(mid_turn="cancel")
    await channel._route_inbound_event(*_member("U0NEW", "Ev1"))

    result = await channel._handle_slack_event(
        *_message("U0OTHER", "Ev2"), is_dm=False, trigger="mention"
    )

    assert result.startswith("queued:")
    assert [request.user_id for request in requests] == ["U0NEW"]
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0NEW", "U0OTHER"]
    assert "member_joined_channel" not in requests[1].params["content"]


@pytest.mark.asyncio
async def test_message_waits_during_join_admission(monkeypatch) -> None:
    channel, requests = _channel(mid_turn="cancel")
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_join(key: str) -> bool:
        if key.endswith("Ev1"):
            entered.set()
            await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_join)
    join = asyncio.create_task(channel._route_inbound_event(*_member("U0NEW", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        result = await asyncio.wait_for(
            channel._handle_slack_event(
                *_message("U0OTHER", "Ev2"), is_dm=False, trigger="mention"
            ), 1,
        )
        assert result.startswith("queued:")
        assert requests == []
    finally:
        release.set()
    await asyncio.wait_for(join, 1)
    assert [request.user_id for request in requests] == ["U0NEW"]
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0NEW", "U0OTHER"]


@pytest.mark.asyncio
async def test_message_then_join_preserves_two_actors_and_turn_order() -> None:
    channel, requests = _channel()
    await channel._handle_slack_event(
        *_message("U0OTHER", "Ev1"), is_dm=False, trigger="mention"
    )

    result = await channel._route_inbound_event(*_member("U0NEW", "Ev2"))

    assert result.startswith("held:member")
    assert [request.user_id for request in requests] == ["U0OTHER"]
    assert _status(channel).pending_events == 1
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0OTHER", "U0NEW"]
    assert requests[1].metadata["slack_user_id"] == "U0NEW"


@pytest.mark.asyncio
async def test_pin_waits_for_its_own_anchored_turn() -> None:
    channel, requests = _channel(family="pin")
    await channel._route_inbound_event(*_pin("U0A", "Ev1"))

    result = await channel._route_inbound_event(*_pin("U0B", "Ev2"))

    assert result.startswith("held:pin")
    assert [request.user_id for request in requests] == ["U0A"]
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0A", "U0B"]
    assert requests[1].metadata["slack_message_ts"] == "1710000000.000100"
    assert requests[1].metadata["slack_thread_ts"] == "1710000000.000100"
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_replayed_held_event_is_deduplicated_before_enqueue() -> None:
    channel, requests = _channel()
    await channel._route_inbound_event(*_member("U0A", "Ev1"))
    event = _member("U0B", "Ev2")

    assert (await channel._route_inbound_event(*event)).startswith("held:member")
    assert (await channel._route_inbound_event(*event)).startswith(
        "ignored:already-handled"
    )
    assert _status(channel).pending_events == 1
    await _finish(channel, requests[0])
    assert [request.user_id for request in requests] == ["U0A", "U0B"]


@pytest.mark.asyncio
async def test_full_and_expired_backlog_counts_loss(monkeypatch) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    monkeypatch.setattr(slack_inbound, "_MAX_PENDING_EVENTS_PER_SESSION", 1)
    await channel._route_inbound_event(*_member("U0A", "Ev1"))
    await channel._route_inbound_event(*_member("U0B", "Ev2"))

    assert (await channel._route_inbound_event(*_member("U0C", "Ev3"))).startswith(
        "dropped:pending-member-full"
    )
    assert (_status(channel).pending_events, _status(channel).lost_events) == (1, 1)
    channel._pending_events[requests[0].session_id][0].queued_at -= (
        slack_inbound._PENDING_EVENT_TIMEOUT_SECONDS + 1
    )
    channel._prune_pending_events()
    assert (_status(channel).pending_events, _status(channel).lost_events) == (0, 2)
    assert channel._inbound_events.fold("C1") is None
    assert any("Ev3" in line and "admission-full" in line for line in warnings)
    assert any("Ev2" in line and "expired" in line for line in warnings)
    assert all(
        "recover manually" in line
        for line in warnings
        if "event turn lost" in line
    )


@pytest.mark.asyncio
async def test_stop_logs_held_event_identity_before_discarding_it(monkeypatch) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    await channel._route_inbound_event(*_member("U0A", "Ev1"))
    await channel._route_inbound_event(*_member("U0B", "Ev2"))
    assert _status(channel).pending_events == 1

    await channel.stop()

    assert requests[0].user_id == "U0A"
    assert not channel._pending_events
    assert any(
        "Ev2" in line
        and "U0B" in line
        and "channel-stopped" in line
        and "recover manually" in line
        for line in warnings
    )


@pytest.mark.asyncio
async def test_stop_during_deduplication_logs_event_without_dispatch(monkeypatch) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel._remember_event

    async def wait_for_event(key: str) -> bool:
        entered.set()
        await release.wait()
        return await remember(key)

    monkeypatch.setattr(channel, "_remember_event", wait_for_event)
    task = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    assert _status(channel).pending_events == 1
    try:
        await channel.stop()
    finally:
        release.set()
    assert (await task).startswith("dropped:member-channel-stopped")
    assert requests == []
    assert not channel._admitting_events
    assert not channel._event_admissions
    assert any(
        all(part in line for part in (
            "Ev1", "U0A", "C1", "channel-stopped-during-admission",
            "recover manually",
        ))
        for line in warnings
    )


@pytest.mark.asyncio
async def test_cancelled_admission_logs_event_and_releases_reservation(monkeypatch) -> None:
    channel, requests = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    entered = asyncio.Event()

    async def wait_for_event(key: str) -> bool:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(channel, "_remember_event", wait_for_event)
    task = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert requests == []
    assert not channel._admitting_events
    assert not channel._event_admissions
    assert any("Ev1" in line and "admission-cancelled" in line for line in warnings)


@pytest.mark.asyncio
async def test_stop_during_dispatch_logs_event_once(monkeypatch) -> None:
    channel, _ = _channel()
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_dispatch(request) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(channel, "_route_request", fail_dispatch)
    task = asyncio.create_task(channel._route_inbound_event(*_member("U0A", "Ev1")))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await channel.stop()
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="dispatch failed"):
        await task
    assert len([line for line in warnings if "Slack event turn lost" in line]) == 1


@pytest.mark.asyncio
async def test_required_reply_downgrades_with_one_warning(monkeypatch) -> None:
    channel, requests = _channel(reply="required")
    warnings = []
    monkeypatch.setattr(
        slack_connect.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )

    for event in (_member("U0A", "Ev1"), _member("U0B", "Ev2")):
        assert (await channel._route_inbound_event(*event)).startswith(
            "buffered:member"
        )

    assert requests == []
    assert len(channel._inbound_events.fold("C1")["events"]) == 2
    assert len([line for line in warnings if "delivery.reply is required" in line]) == 1
