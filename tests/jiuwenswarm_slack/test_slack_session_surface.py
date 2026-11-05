"""Belle's ``slack.session_surface`` picks the thread status API.

Slack has two families for one line of text. ``assistant.threads.setStatus``
takes the operator's own words and takes the status down on its own.
``agents.sessions.setStatus`` takes one of a fixed set of lifecycle words and
takes nothing down: a turn that ends has to say ``active``, or the thread stays
in ``processing`` for an hour.

``auto`` is the default word, and it reads the configuration before it reads
the workspace. ``agents.sessions.setStatus`` draws none of the operator's own
status wording, so a channel that wrote ``queued_status`` or ``steered_status``
answers ``assistant`` and makes no probe at all. Both words as shipped means
there is nothing to lose, and the probe runs.

Which family is usable is a property of the workspace and of this app
installation, and both can change while the process runs. So the memo these
tests pin is revisable: a refusal has to move it at the call it arrives on
rather than at the next restart. The reverse does not happen inside a process.

The classification of a refusal is the other half. Falling back on every error
would hide a wrong call behind an older method that keeps working, so only the
codes that say "this app cannot use this family here" move the memo, and that
set is an allow-list. A caller error, a transient failure, a token refusal and
an unclassified code each stay on the newer method and are logged at the level
that suits them.

Also covered: the four config words, the lost ``queued_status`` and
``steered_status`` wording that the newer family costs and the startup line
that reports it, and the end-of-turn call, which stops being harmless there.
"""

from belle_agent.platforms.slack.thread_status import _DEFAULT_QUEUED_STATUS
from belle_agent.platforms.slack.thread_status import _DEFAULT_STEERED_STATUS

from belle_agent.platforms.slack import thread_status
from belle_agent.platforms.slack.thread_status import AGENTS_STATUS_ACTIVE, AGENTS_STATUS_PROCESSING

import logging
from typing import Any

import pytest

from belle_agent.platforms.slack.admission import SlackMessageOrigin
from belle_agent.platforms.slack.config import SESSION_SURFACE_DEFAULT, SESSION_SURFACE_WORDS

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.platforms.slack.config import SESSION_SURFACE_AGENTS, SESSION_SURFACE_ASSISTANT, SESSION_SURFACE_AUTO, SESSION_SURFACE_OFF, resolve_session_surface
from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannel, SlackChannelConfig

AGENTS = thread_status._AGENTS_SET_STATUS_METHOD
ASSISTANT = thread_status._ASSISTANT_SET_STATUS_METHOD

CHANNEL = "C0CHANNEL1"
THREAD = "1710000000.000100"
# A channel message answered in its own thread. Slack has drawn that thread, so
# a status is sent here whatever else is configured, which leaves which API
# sends it as the only question these tests ask.
IN_A_THREAD = SlackMessageOrigin(
    channel_id=CHANNEL,
    message_ts=THREAD,
    reply_thread_ts=THREAD,
    root_thread_ts=THREAD,
)

THINKING = "is thinking…"


def _lines_at(caplog, level: int, *, logger: str = "") -> list[str]:
    names = {logger} if logger else {slack_connect.__name__, thread_status.__name__}
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name in names
    ]


class _Refused(Exception):
    """A Slack refusal shaped the way ``slack_error_code`` reads one.

    Slack answers a refusal with HTTP 200 and ``ok: false``, so the code lives
    in the body. ``slack_error_code`` reads ``exc.response`` as a mapping, which
    is all this needs to be.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class _StatusClient:
    """Records every status call, and refuses the methods it is told to."""

    def __init__(self, *, refuse: dict[str, str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._refuse = dict(refuse or {})

    def refuse(self, method: str, code: str) -> None:
        """Start refusing ``method`` part-way through a process."""
        self._refuse[method] = code

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        self.calls.append({"api_method": api_method, **kwargs})
        code = self._refuse.get(api_method)
        if code:
            raise _Refused(code)
        return {"ok": True}

    def methods(self) -> list[str]:
        return [call["api_method"] for call in self.calls]

    def statuses(self, method: str) -> list[str]:
        return [
            str(call["data"]["status"])
            for call in self.calls
            if call["api_method"] == method
        ]


def _channel(client: _StatusClient | None = None, **overrides: Any) -> SlackChannel:
    overrides.setdefault("thinking_status", THINKING)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    channel._client = client if client is not None else _StatusClient()
    return channel


# --------------------------------------------------------------------------
# the four words
# --------------------------------------------------------------------------


def test_the_default_reads_the_wording_before_it_reads_the_workspace() -> None:
    """An unwritten key resolves to ``auto``.

    ``auto`` costs no operator any word they wrote, which is what lets it be
    the default. The word it lands on is settled per channel, not here.
    """
    assert SESSION_SURFACE_DEFAULT == SESSION_SURFACE_AUTO
    assert resolve_session_surface({}) == SESSION_SURFACE_AUTO
    assert SlackChannelConfig().session_surface == SESSION_SURFACE_AUTO


@pytest.mark.asyncio
async def test_a_config_that_says_nothing_reaches_the_newer_api() -> None:
    """The default wording is shipped wording, so nothing is lost by probing."""
    client = _StatusClient()
    channel = _channel(client)

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS]


@pytest.mark.parametrize("word", SESSION_SURFACE_WORDS)
def test_every_word_survives_the_reader(word: str) -> None:
    assert resolve_session_surface({"session_surface": word}) == word
    assert resolve_session_surface({"session_surface": word.upper()}) == word
    assert resolve_session_surface({"session_surface": f"  {word} "}) == word


def test_a_yaml_boolean_off_is_read_as_the_word_it_was_written_as() -> None:
    """YAML 1.1 loads a bare ``off`` as ``False`` long before it is a word.

    Reading that as a plain string would turn "draw no status" into the default
    that draws one, which is the trap ``render_tables`` documents next door.
    """
    assert resolve_session_surface({"session_surface": False}) == SESSION_SURFACE_OFF


def test_a_yaml_boolean_true_names_no_word_and_is_not_guessed_at() -> None:
    """``on``, ``yes`` and ``true`` all reach the reader as ``True``."""
    assert resolve_session_surface({"session_surface": True}) == SESSION_SURFACE_DEFAULT


def test_a_misspelling_warns_and_falls_to_the_default(caplog) -> None:
    assert (
        resolve_session_surface({"session_surface": "agent"})
        == SESSION_SURFACE_DEFAULT
    )
    warnings = _lines_at(
        caplog, logging.WARNING, logger="belle_agent.platforms.slack.config"
    )
    assert any("session_surface='agent'" in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_assistant_calls_the_older_api_with_the_operators_words() -> None:
    client = _StatusClient()
    channel = _channel(client, session_surface=SESSION_SURFACE_ASSISTANT)

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [ASSISTANT]
    assert client.statuses(ASSISTANT) == [THINKING]


@pytest.mark.asyncio
async def test_agents_calls_the_newer_api_with_a_lifecycle_word() -> None:
    client = _StatusClient()
    channel = _channel(client, session_surface=SESSION_SURFACE_AGENTS)

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS]
    assert client.statuses(AGENTS) == [AGENTS_STATUS_PROCESSING]


@pytest.mark.asyncio
async def test_off_sends_nothing_at_all() -> None:
    """Neither API, on any of the four states a status is written for."""
    client = _StatusClient()
    channel = _channel(
        client,
        session_surface=SESSION_SURFACE_OFF,
        queued_status="held",
        steered_status="folded in",
    )

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_queued_status(IN_A_THREAD)
    await channel._set_steered_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)

    assert client.calls == []


@pytest.mark.asyncio
async def test_agents_maps_the_four_states_onto_two_lifecycle_words() -> None:
    """Queued and steered stop being distinguishable, which is the cost.

    The end of a turn is ``active`` and never ``closed``. ``closed`` ends the
    conversation rather than the turn, and a session closed after every turn
    would refuse to answer the next message.
    """
    client = _StatusClient()
    channel = _channel(
        client,
        session_surface=SESSION_SURFACE_AGENTS,
        queued_status="held",
        steered_status="folded in",
    )

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_queued_status(IN_A_THREAD)
    await channel._set_steered_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)

    assert client.statuses(AGENTS) == [
        AGENTS_STATUS_PROCESSING,
        AGENTS_STATUS_ACTIVE,
        AGENTS_STATUS_PROCESSING,
        AGENTS_STATUS_ACTIVE,
    ]
    assert "closed" not in client.statuses(AGENTS)
    # Neither configured wording reached Slack.
    assert "held" not in str(client.calls)
    assert "folded in" not in str(client.calls)


@pytest.mark.asyncio
async def test_a_blank_thinking_status_still_turns_the_status_off() -> None:
    """Empty is the off switch on both surfaces, not only on the older one."""
    client = _StatusClient()
    channel = _channel(
        client, session_surface=SESSION_SURFACE_AGENTS, thinking_status=""
    )

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.calls == []


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(thread_status._AGENTS_UNAVAILABLE))
@pytest.mark.asyncio
async def test_auto_probes_once_and_falls_back_on_every_unavailable_code(
    code: str, slack_logs
) -> None:
    """The first status call of the process is the probe.

    Nothing in the config can answer this. ``auth.test`` reports scopes and
    identity, and whether this app may use the newer family is neither, so the
    call is the only question with an answer. The cause does not change the
    answer, which is why every code in the set is treated the same way: a
    workspace feature that is off, an app that is not configured for it, a grant
    that was never made, an org policy. The refused status is reissued rather
    than dropped, so the thread still gets its line.
    """
    client = _StatusClient(refuse={AGENTS: code})
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, ASSISTANT]
    assert client.statuses(ASSISTANT) == [THINKING]
    assert channel._agents_status_allowed is False
    assert any(code in line for line in _lines_at(slack_logs, logging.INFO))


@pytest.mark.asyncio
async def test_the_probe_is_not_repeated_after_it_has_been_answered() -> None:
    """One refusal settles it. Every later call goes straight to the fallback."""
    client = _StatusClient(refuse={AGENTS: "feature_disabled"})
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_thinking_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, ASSISTANT, ASSISTANT, ASSISTANT]


@pytest.mark.asyncio
async def test_auto_stays_on_the_newer_api_while_the_workspace_allows_it() -> None:
    client = _StatusClient()
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, AGENTS]
    assert channel._agents_status_allowed is True


@pytest.mark.parametrize(
    "code",
    sorted(
        thread_status._AGENTS_CALLER_ERRORS
        | thread_status._AGENTS_TRANSIENT
        | thread_status._AGENTS_TOKEN_REFUSALS
        | {"a_code_slack_has_not_invented_yet"}
    ),
)
@pytest.mark.asyncio
async def test_a_refusal_that_is_not_unavailability_does_not_move_the_memo(
    code: str,
) -> None:
    """Three other groups, and everything nobody classified.

    A caller error means this connector called the method wrongly, and falling
    back would make that defect permanent and invisible. A transient failure and
    a token refusal say nothing about availability, and the second would fail
    twice because both methods read one token. An unrecognised code falls
    outside the allow-list, which is what makes the allow-list safe.
    """
    client = _StatusClient(refuse={AGENTS: code})
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS]
    assert channel._agents_status_allowed is True


def test_the_four_refusal_groups_do_not_overlap() -> None:
    """One code, one reading. An overlap would make the order of tests matter."""
    groups = (
        thread_status._AGENTS_UNAVAILABLE,
        thread_status._AGENTS_CALLER_ERRORS,
        thread_status._AGENTS_TRANSIENT,
        thread_status._AGENTS_TOKEN_REFUSALS,
    )
    seen: set[str] = set()
    for group in groups:
        assert not (seen & group), sorted(seen & group)
        seen |= group


@pytest.mark.parametrize(
    "code, level",
    [
        ("invalid_status", logging.WARNING),
        ("thread_ts_required", logging.WARNING),
        ("a_code_slack_has_not_invented_yet", logging.WARNING),
        ("ratelimited", logging.DEBUG),
        ("token_revoked", logging.DEBUG),
    ],
)
@pytest.mark.asyncio
async def test_a_refusal_is_logged_at_the_level_its_group_earns(
    code: str, level: int, slack_logs
) -> None:
    client = _StatusClient(refuse={AGENTS: code})
    channel = _channel(client, session_surface=SESSION_SURFACE_AGENTS)

    await channel._set_thinking_status(IN_A_THREAD)

    assert any(code in line for line in _lines_at(slack_logs, level))
    other = logging.DEBUG if level == logging.WARNING else logging.WARNING
    assert not any(code in line for line in _lines_at(slack_logs, other))


@pytest.mark.asyncio
async def test_an_unavailable_refusal_falls_back_under_the_explicit_word_too() -> None:
    """The memo answers a question about Slack, not about the operator.

    An operator who wrote ``agents`` asked for the better status line, not for
    no status line at all, so an installation that cannot use the family falls
    back under that word as well.
    """
    client = _StatusClient(refuse={AGENTS: "not_agent_app"})
    channel = _channel(client, session_surface=SESSION_SURFACE_AGENTS)

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, ASSISTANT, ASSISTANT]


@pytest.mark.asyncio
async def test_the_memo_flips_mid_process_when_a_working_family_stops_working(
    slack_logs,
) -> None:
    """Days of successful calls do not make the answer permanent.

    Availability is a property of the workspace and of this app installation,
    and both can be changed while the process runs, so a call that worked
    yesterday can be refused today. The fallback happens at that call.
    """
    client = _StatusClient()
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)
    assert client.methods() == [AGENTS, AGENTS]

    client.refuse(AGENTS, "feature_disabled")
    await channel._set_thinking_status(IN_A_THREAD)
    await channel._clear_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, AGENTS, AGENTS, ASSISTANT, ASSISTANT]
    assert channel._agents_status_allowed is False
    # Each reissued status is the one that was refused, in order.
    assert client.statuses(ASSISTANT) == [THINKING, ""]
    assert any(
        "feature_disabled" in line for line in _lines_at(slack_logs, logging.INFO)
    )


@pytest.mark.asyncio
async def test_the_flip_is_said_once_and_not_on_every_later_call(
    slack_logs,
) -> None:
    client = _StatusClient(refuse={AGENTS: "feature_disabled"})
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO)

    for _ in range(4):
        await channel._set_thinking_status(IN_A_THREAD)

    flips = [
        line
        for line in _lines_at(slack_logs, logging.INFO)
        if "feature_disabled" in line
    ]
    assert len(flips) == 1, flips


def test_the_memo_moves_in_both_directions_when_it_is_told_to(slack_logs) -> None:
    """Only one direction happens inside a process, and the setter is not why.

    A refusal flips the memo at the call it arrives on. A family that becomes
    usable again waits for the next restart, because losing it breaks a working
    status line and gaining it only adds a nicety. The setter itself reports
    either move, so a later caller that wants the other direction gets a log
    line for it.
    """
    channel = _channel()

    channel._remember_agents_status(False, "feature_disabled")
    channel._remember_agents_status(True, "a probe answered")

    reported = [
        line
        for line in _lines_at(slack_logs, logging.INFO)
        if "thread status moves to" in line
    ]
    assert len(reported) == 2, reported
    assert ASSISTANT in reported[0]
    assert AGENTS in reported[1]


@pytest.mark.asyncio
async def test_the_memo_is_per_channel_and_not_shared(slack_logs) -> None:
    """Availability is a property of one workspace and one app installation."""
    refusing = _channel(
        _StatusClient(refuse={AGENTS: "feature_disabled"}),
        session_surface=SESSION_SURFACE_AUTO,
    )
    allowing_client = _StatusClient()
    allowing = _channel(allowing_client, session_surface=SESSION_SURFACE_AUTO)

    await refusing._set_thinking_status(IN_A_THREAD)
    await allowing._set_thinking_status(IN_A_THREAD)

    assert refusing._agents_status_allowed is False
    assert allowing._agents_status_allowed is True
    assert allowing_client.methods() == [AGENTS]


# --------------------------------------------------------------------------
# the two consequences of the newer API
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_end_of_turn_is_a_warning_under_the_newer_api(
    slack_logs,
) -> None:
    """That API takes nothing down on its own.

    A thread whose end-of-turn status did not land sits in ``processing`` for an
    hour, where the same miss against the older API is dropped by Slack's own
    timeout within two minutes. ``not_in_channel`` is used here because it is
    also the one requirement the newer method adds: this app must be a member of
    the conversation. It is not classified as unavailability, so it is reported
    rather than absorbed.
    """
    client = _StatusClient(refuse={AGENTS: "not_in_channel"})
    channel = _channel(client, session_surface=SESSION_SURFACE_AGENTS)

    await channel._clear_thinking_status(IN_A_THREAD)

    warnings = _lines_at(slack_logs, logging.WARNING)
    assert any(AGENTS in line and "clear" in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_a_failed_transient_set_stays_at_debug_under_the_newer_api(
    slack_logs,
) -> None:
    """Only the end of a turn is the call that cannot be left to a timeout."""
    client = _StatusClient(refuse={AGENTS: "ratelimited"})
    channel = _channel(client, session_surface=SESSION_SURFACE_AGENTS)

    await channel._set_thinking_status(IN_A_THREAD)

    assert _lines_at(slack_logs, logging.WARNING) == []
    assert any(AGENTS in line for line in _lines_at(slack_logs, logging.DEBUG))


@pytest.mark.asyncio
async def test_a_failed_end_of_turn_stays_at_debug_under_the_older_api(
    slack_logs,
) -> None:
    """Slack drops that status on its own, so a missed call costs minutes."""
    client = _StatusClient(refuse={ASSISTANT: "not_in_channel"})
    channel = _channel(client, session_surface=SESSION_SURFACE_ASSISTANT)

    await channel._clear_thinking_status(IN_A_THREAD)

    assert _lines_at(slack_logs, logging.WARNING) == []
    assert any(ASSISTANT in line for line in _lines_at(slack_logs, logging.DEBUG))


# --------------------------------------------------------------------------
# the wording decides auto
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["queued_status", "steered_status"])
@pytest.mark.asyncio
async def test_auto_keeps_the_operators_own_words_and_never_probes(
    key: str,
) -> None:
    """One written word is enough, and the workspace is never asked.

    ``agents.sessions.setStatus`` draws four fixed words, so a workspace that
    would accept the call still would not draw what the operator wrote. There
    is nothing a probe could answer that changes the better outcome, so no
    probe is made.
    """
    client = _StatusClient()
    channel = _channel(client, session_surface=SESSION_SURFACE_AUTO, **{key: "held"})

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_queued_status(IN_A_THREAD)
    await channel._set_steered_status(IN_A_THREAD)

    assert AGENTS not in client.methods()
    assert client.methods() == [ASSISTANT] * 3
    # The memo was never asked and never moved.
    assert channel._agents_status_allowed is True


@pytest.mark.asyncio
async def test_auto_probes_where_both_words_are_the_shipped_ones() -> None:
    """Written back verbatim is not a choice, and still gets the newer API.

    The comparison is against the shipped defaults and not against emptiness.
    An operator who typed the template line into their own file has decided
    nothing that the newer family would take away.
    """
    client = _StatusClient()
    channel = _channel(
        client,
        session_surface=SESSION_SURFACE_AUTO,
        queued_status=_DEFAULT_QUEUED_STATUS,
        steered_status=_DEFAULT_STEERED_STATUS,
    )

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS]


@pytest.mark.parametrize(
    "value",
    [
        f"  {_DEFAULT_QUEUED_STATUS} ",
        f"\t{_DEFAULT_QUEUED_STATUS}\n",
        "",
        "   ",
    ],
)
@pytest.mark.asyncio
async def test_auto_probes_where_the_difference_is_whitespace_or_nothing(
    value: str,
) -> None:
    """Neither spacing nor a blank is wording an operator would lose.

    The value is stripped before it is sent, so the shipped line with a space
    around it reaches Slack as the shipped line. A blank sends no status at
    all, on either family.
    """
    client = _StatusClient()
    channel = _channel(
        client, session_surface=SESSION_SURFACE_AUTO, queued_status=value
    )

    await channel._set_thinking_status(IN_A_THREAD)

    assert client.methods() == [AGENTS]


@pytest.mark.asyncio
async def test_agents_still_costs_the_wording_when_it_is_asked_for() -> None:
    """The explicit word is unchanged. It reads the memo and not the config."""
    client = _StatusClient()
    channel = _channel(
        client, session_surface=SESSION_SURFACE_AGENTS, queued_status="held"
    )

    await channel._set_thinking_status(IN_A_THREAD)
    await channel._set_queued_status(IN_A_THREAD)

    assert client.methods() == [AGENTS, AGENTS]
    assert "held" not in str(client.calls)


@pytest.mark.parametrize("word", [SESSION_SURFACE_ASSISTANT, SESSION_SURFACE_OFF])
@pytest.mark.asyncio
async def test_the_other_two_words_do_not_read_the_wording_either(
    word: str,
) -> None:
    """``assistant`` draws the words and ``off`` draws nothing, as before."""
    client = _StatusClient()
    channel = _channel(client, session_surface=word, queued_status="held")

    await channel._set_queued_status(IN_A_THREAD)

    expected = [] if word == SESSION_SURFACE_OFF else [ASSISTANT]
    assert client.methods() == expected


# --------------------------------------------------------------------------
# the startup notice
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("queued_status", "is holding this one"),
        ("steered_status", "is folding this one in"),
    ],
)
def test_customised_wording_with_the_newer_api_is_reported_at_startup(
    key: str, value: str, slack_logs
) -> None:
    """Said once at startup, not discovered weeks later in a thread."""
    channel = _channel(session_surface=SESSION_SURFACE_AGENTS, **{key: value})

    channel._log_session_surface()

    warnings = _lines_at(slack_logs, logging.WARNING)
    assert any(f"Belle platforms.slack.{key}" in line for line in warnings), warnings
    assert any(AGENTS in line for line in warnings), warnings


@pytest.mark.parametrize(
    "key, value",
    [
        ("queued_status", "is holding this one"),
        ("steered_status", "is folding this one in"),
    ],
)
def test_auto_says_at_info_which_family_the_wording_chose(
    key: str, value: str, slack_logs
) -> None:
    """INFO and not a warning, because nothing was lost.

    An operator hunting for the newer status line in a workspace that supports
    it has nothing else to read that names what stopped it, so the line names
    the key that decided and the word to write for the other answer.
    """
    channel = _channel(session_surface=SESSION_SURFACE_AUTO, **{key: value})

    channel._log_session_surface()

    assert _lines_at(slack_logs, logging.WARNING) == []
    infos = _lines_at(slack_logs, logging.INFO)
    assert len(infos) == 1, infos
    assert "session_surface=auto resolved to assistant" in infos[0]
    assert f"Belle platforms.slack.{key}" in infos[0]
    assert "session_surface: agents" in infos[0]


def test_auto_with_the_shipped_wording_says_nothing_at_all(slack_logs) -> None:
    """The memo decides there, and it reports every move it makes itself."""
    channel = _channel(session_surface=SESSION_SURFACE_AUTO)

    channel._log_session_surface()

    assert _lines_at(slack_logs, logging.WARNING) == []
    assert _lines_at(slack_logs, logging.INFO) == []


def test_both_customised_keys_are_named_in_one_line(slack_logs) -> None:
    channel = _channel(
        session_surface=SESSION_SURFACE_AGENTS,
        queued_status="is holding this one",
        steered_status="is folding this one in",
    )

    channel._log_session_surface()

    warnings = _lines_at(slack_logs, logging.WARNING)
    assert len(warnings) == 1, warnings
    assert "Belle platforms.slack.queued_status" in warnings[0]
    assert "Belle platforms.slack.steered_status" in warnings[0]


def test_the_defaults_lose_no_wording_and_say_nothing(slack_logs) -> None:
    """A line on every start about wording nobody wrote would be noise."""
    channel = _channel(session_surface=SESSION_SURFACE_AGENTS)

    channel._log_session_surface()

    assert _lines_at(slack_logs, logging.WARNING) == []
    assert _lines_at(slack_logs, logging.INFO) == []


@pytest.mark.parametrize("word", [SESSION_SURFACE_ASSISTANT, SESSION_SURFACE_OFF])
def test_the_other_two_words_lose_no_wording_either(word: str, slack_logs) -> None:
    channel = _channel(
        session_surface=word,
        queued_status="is holding this one",
        steered_status="is folding this one in",
    )

    channel._log_session_surface()

    assert _lines_at(slack_logs, logging.WARNING) == []
    assert _lines_at(slack_logs, logging.INFO) == []


def test_the_shipped_defaults_are_what_customised_is_measured_against() -> None:
    """The keys a channel reports as written, and what counts as written."""
    assert _channel()._customised_status_wording() == ()
    assert _channel(
        queued_status=f" {_DEFAULT_QUEUED_STATUS}  ",
        steered_status="",
    )._customised_status_wording() == ()
    assert _channel(
        queued_status="held", steered_status="folded in"
    )._customised_status_wording() == ("queued_status", "steered_status")
