"""Test people, roles, and role-based matching."""

from dataclasses import fields
from typing import Any

import pytest
import yaml

from belle_agent.scopes import (
    ChannelCapabilities,
    PeopleDirectory,
    Scope,
    ScopeMatch,
    compile_people,
    compile_scopes,
    matching_scopes,
    register_channel,
)
from belle_agent.scopes import capabilities as caps


@pytest.fixture
def demo_channel(registry):
    """A channel that reads everything and names a sender."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, "agent": None, "permissions": None},
            identity_keys=("user",),
        )
    )


@pytest.fixture
def quiet_channel(registry):
    """A channel that reads sections but identifies no sender."""
    registry(
        ChannelCapabilities(
            channel="quiet",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
            identity_keys=(),
        )
    )


PEOPLE = {
    "hana": {"demo": "U_HAN", "other": "O_HAN"},
    "boss": {"demo": "U_BOSS"},
    "remote": {"other": "O_REMOTE"},
}
ROLES = {"admin": ["hana", "boss"], "elsewhere": ["remote"]}


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    kwargs.setdefault("people", PEOPLE)
    kwargs.setdefault("roles", ROLES)
    return compile_scopes(entries, warn=warn, **kwargs)


# The directory itself


def test_a_person_is_a_name_with_one_id_per_platform():
    directory = compile_people(PEOPLE, ROLES)

    assert directory.knows_role("admin")
    assert directory.members("admin") == ("boss", "hana")
    assert directory.ids_for_role("admin", channel="demo") == ("U_BOSS", "U_HAN")
    # The same role on another platform is a different set of ids, which is why
    # a role cannot be resolved without knowing which platform is being asked.
    assert directory.ids_for_role("admin", channel="other") == ("O_HAN",)


def test_a_platform_may_carry_several_ids_for_one_person():
    directory = compile_people({"carol": {"feishu": ["ou_a1b2c3", "on_d4e5f6"]}}, {"team": ["carol"]})

    assert directory.ids_for_role("team", channel="feishu") == ("on_d4e5f6", "ou_a1b2c3")


def test_neither_block_written_is_an_empty_directory():
    directory = compile_people(None, None)

    assert directory.roles == {}
    assert not directory.knows_role("admin")


def test_an_id_is_stripped_and_deduplicated():
    directory = compile_people({"alice": {"demo": [" U1 ", "U1", "U2"]}}, {"team": ["alice"]})

    assert directory.ids_for_role("team", channel="demo") == ("U1", "U2")


def test_a_platform_this_deployment_does_not_run_is_accepted():
    directory = compile_people({"alice": {"a-platform-nobody-declared": "X1"}}, None)

    assert directory.people == {"alice": {"a-platform-nobody-declared": ("X1",)}}


# Entries that stop Belle from starting


def test_an_unquoted_numeric_id_stops_belle_from_starting(refusal):
    config = yaml.safe_load("people:\n  ghost: {discord: 123456789012345678}\nroles:\n  team: [ghost]\n")

    error = refusal(None, people=config["people"], roles=config["roles"])

    assert error == (
        "Belle config people and roles are not valid. Correct these entries:\n"
        "people.ghost.discord=123456789012345678 is not an id or a list of ids."
        " Quote each id so that YAML keeps it as text"
    )


@pytest.mark.parametrize(
    ("people", "roles", "expected"),
    [
        ({"alice": {"demo": ["", "U1"]}}, None, "people.alice.demo contains an empty id"),
        ({"alice": {"demo": None}}, None, "people.alice.demo=None is not an id or a list of ids"),
        ({"alice": "U1"}, None, "people.alice='U1' is not a mapping. Write {platform: id}"),
        ({7: {"demo": "U1"}}, None, "people has the key 7. Write each name as text"),
        ({"alice": {"demo": "U1"}, " alice": {"demo": "U2"}}, None, "people names alice more than once. Write each name once"),
        ({"alice": {"demo": "U1", "demo ": "U2"}}, None, "people.alice names demo more than once. Write each platform name once"),
        ({"alice": {"demo": "U1"}}, {"admin": ["alice"], "admin ": ["alice"]}, "roles names admin more than once"),
        (["alice"], None, "people=['alice'] is not a mapping"),
        (
            {"alice": {"demo": "U1"}},
            {"admin": {"people": ["alice"]}},
            "roles.admin is a mapping. A role is a list of names from people."
            " Put what the role can do in a scope that matches role: admin",
        ),
        ({"alice": {"demo": "U1"}}, {"admin": "alice"}, "roles.admin='alice' is one name. Write a list of names: [alice]"),
        ({"alice": {"demo": "U1"}}, {"admin": []}, "roles.admin is an empty list"),
        ({"alice": {"demo": "U1"}}, {"admin": ["alice", "nobody"]}, "roles.admin contains nobody, which is not in people"),
        ({"alice": {"demo": "U1"}}, {"admin": ["alice"], "all": ["admin"]}, "roles.all contains the role admin"),
        ({"alice": {"demo": "U1"}}, {"admin": [None]}, "roles.admin contains None, which is not a name"),
    ],
)
def test_an_invalid_entry_stops_belle_from_starting(refusal, people, roles, expected):
    assert expected in refusal(None, people=people, roles=roles)


def test_the_refusal_names_every_invalid_entry_once(refusal):
    error = refusal(None, people={"alice": "U1", "bob": {"demo": [5]}}, roles={"admin": ["alice", "bob"]})

    assert error.splitlines()[1:] == [
        "people.alice='U1' is not a mapping. Write {platform: id}",
        "people.bob.demo=[5] is not an id or a list of ids. Quote each id so that YAML keeps it as text",
    ]


# The role axis


def test_a_role_selects_the_people_in_it(demo_channel, warn, warnings):
    scopes = _compile(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_HAN")
    assert scopes[0].selects(channel="demo", chat="C1", user="U_BOSS")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_role_is_resolved_into_the_ids_user_would_have_enumerated(
    demo_channel, warn, warnings
):
    by_role = _compile(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )
    by_id = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U_HAN", "U_BOSS"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Stated as an equality: a role is sugar over user for the matcher, so the
    # two spellings must resolve to the same ids and not merely behave alike.
    assert by_role[0].match.users == by_id[0].match.users

    assert by_role[0].layer == (1, 0, 0, 1)
    assert by_id[0].layer == (1, 0, 0, 2)


def test_user_and_role_or_rather_than_and(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U_GUEST"], "role": "admin"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # AND would mean "U_GUEST, but only if also an admin", which nobody wants
    # and which a config author would write by accident.
    assert warnings == []
    for sender in ("U_GUEST", "U_HAN", "U_BOSS"):
        assert scopes[0].selects(channel="demo", chat="C1", user=sender), sender
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_not_on_a_role_is_the_read_only_except_admins_rule(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "not": {"role": "admin"}},
                "delivery": {"prompt": "Read-only here."},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAN")


def test_a_not_may_exclude_ids_and_a_role_together(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "not": {"user": ["U_GUEST"], "role": "admin"},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The union, not the last key written. Both spellings name one axis, so a
    # not: block naming both excludes both.
    assert scopes[0].match.not_users == ("U_BOSS", "U_GUEST", "U_HAN")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_GUEST")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAN")
    assert scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_role_minus_one_person_is_expressible(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "role": "admin",
                    "not": {"user": ["U_BOSS"]},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_HAN")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_BOSS")


def test_several_roles_may_be_named_and_they_or(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "role": ["admin", "elsewhere"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes[0].match.users == ("U_BOSS", "U_HAN")
    assert any("adds nobody on demo" in line for line in warnings)


# Specificity: identity counts once, however it is spelled


def test_a_role_does_not_outrank_an_id_on_the_same_axis(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U_HAN"]},
                "delivery": {"prompt": "by id"},
            },
            {
                "match": {"channel": "demo", "chat": "C1", "role": "admin"},
                "delivery": {"prompt": "by role"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "role": "admin",
                    "not": {"user": ["U_BOSS"]},
                },
                "delivery": {"prompt": "by role, minus one"},
            },
        ],
        warn,
    )

    assert [scope.layer for scope in scopes] == [(1, 0, 2, 2), (1, 0, 2, 1), (1, 0, 2, 1)]

    ordered = matching_scopes(scopes, channel="demo", chat="C1", user="U_HAN")
    assert [scope.index for scope in ordered] == [1, 2, 0]
    assert ordered[-1].section("delivery") == {"prompt": "by id"}


def test_a_chat_scope_outranks_a_role_scope_either_way_round(
    demo_channel, warn, warnings
):
    role_first = _compile(
        [
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "r"}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
        ],
        warn,
    )
    chat_first = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "r"}},
        ],
        warn,
    )

    assert [scope.layer for scope in role_first] == [(1, 0, 0, 1), (1, 0, 2, 0)]
    assert [scope.layer for scope in chat_first] == [(1, 0, 2, 0), (1, 0, 0, 1)]

    def _last(scopes):
        return matching_scopes(scopes, channel="demo", chat="C1", user="U_HAN")[-1]

    assert _last(role_first).section("delivery") == {"prompt": "c"}
    assert _last(chat_first).section("delivery") == {"prompt": "c"}


def test_a_role_that_resolves_to_nobody_here_still_constrains_identity(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "role": "elsewhere"},
                "delivery": {"prompt": "nobody here"},
            }
        ],
        warn,
    )

    assert scopes[0].match.users == ()
    assert scopes[0].layer == (1, 0, 2, 1)
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAN")


# Fail-closed


def test_a_person_with_no_id_here_is_not_in_the_role_here(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "not": {"role": "elsewhere"}},
                # ``delivery`` rather than ``permissions``: the rule under test
                # is addressed on an identity axis, which that section may not
                # be until its consumer is handed a sender.
                "delivery": {"prompt": "Restricted here."},
            }
        ],
        warn,
    )

    assert scopes[0].match.not_users == ()
    assert scopes[0].selects(channel="demo", chat="C1", user="O_REMOTE")
    assert scopes[0].selects(channel="demo", chat="C1", user=None)
    assert any(
        "excludes nobody on demo" in line and "they are identified on other" in line
        for line in warnings
    )


def test_an_unidentified_sender_answers_a_role_the_way_it_answers_an_id(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "in"}},
            {
                "match": {"channel": "demo", "not": {"role": "admin"}},
                "delivery": {"prompt": "out"},
            },
        ],
        warn,
    )

    for absent in (None, "", "   "):
        assert not scopes[0].selects(channel="demo", chat="C1", user=absent)
        assert scopes[1].selects(channel="demo", chat="C1", user=absent)


def test_an_undeclared_role_is_refused_rather_than_naming_nobody(demo_channel, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "not": {"role": "admins"}},
                "permissions": {"tools": {"bash": "deny"}},
            }
        ],
        people=PEOPLE,
        roles=ROLES,
    )

    assert "scopes[0].match names admins, which is not a valid role under roles" in error
    assert "The valid roles are admin, elsewhere" in error


def test_a_role_used_with_no_people_block_at_all_is_refused(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match names admin, which is not a valid role under roles. The valid roles are none" in error


def test_a_role_named_without_a_platform_is_refused(demo_channel, refusal):
    error = refusal(
        [{"match": {"role": "admin"}, "delivery": {"mode": ["all"]}}], people=PEOPLE, roles=ROLES
    )

    assert "scopes[0].match names senders (admin) but no channel. Add channel to the match block" in error


def test_a_role_on_a_channel_that_names_no_sender_warns_in_the_authors_spelling(
    quiet_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "quiet", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert len(scopes) == 1
    assert any("match.role is set but quiet does not identify a sender" in line for line in warnings)


# Shapes the axis refuses


def test_an_empty_role_list_is_refused(demo_channel, refusal):
    error = refusal(
        [{"match": {"channel": "demo", "role": []}, "delivery": {"mode": ["all"]}}], people=PEOPLE, roles=ROLES
    )

    assert "scopes[0].match.role is an empty list. Write at least one role name" in error


def test_a_role_that_is_not_a_name_is_refused(demo_channel, refusal):
    error = refusal(
        [{"match": {"channel": "demo", "role": 7}, "delivery": {"mode": ["all"]}}], people=PEOPLE, roles=ROLES
    )

    assert "scopes[0].match.role=7 is not a role name or a list of role names" in error


def test_supported_axes():
    from belle_agent.scopes import NOT_AXES, SUPPORTED_AXES

    assert SUPPORTED_AXES == (
        "channel",
        "workspace",
        "chat",
        "chat_type",
        "user",
        "role",
    )
    assert NOT_AXES == ("user", "role")


def test_the_description_shows_the_ids_a_role_resolved_to(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "role": "admin"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Both, because they can differ: a role resolving to fewer people than it
    # reads as is the case worth being able to see in a log line.
    described = scopes[0].match.describe()
    assert "role: [admin]" in described
    assert "user: [U_BOSS, U_HAN]" in described


def test_a_directory_can_be_handed_over_already_built(demo_channel, warn, warnings):
    directory = compile_people(PEOPLE, ROLES)
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        directory=directory,
        warn=warn,
    )

    assert isinstance(directory, PeopleDirectory)
    assert scopes[0].match.users == ("U_BOSS", "U_HAN")
    assert warnings == []


def test_role_resolution_carries_every_axis_it_did_not_resolve(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="every",
            axes=frozenset({"channel", "workspace", "chat", "chat_type", "user"}),
            sections={"delivery": None},
            identity_keys=("user",),
            axis_values={"chat_type": frozenset({"room", "direct"})},
        )
    )
    # ``chat`` and ``chat_type`` are refused together, so the axes take two
    # rules between them rather than one rule naming all of them.
    written = (
        {"channel": "every", "workspace": "T1", "chat": "C1"},
        {"channel": "every", "chat_type": "direct"},
    )
    people = {"boss": {"every": "U_BOSS"}}
    roles = {"admin": ["boss"]}
    try:
        with_role = compile_scopes(
            [
                {"match": {**match, "role": "admin"}, "delivery": {"mode": ["all"]}}
                for match in written
            ],
            people=people,
            roles=roles,
            warn=warn,
        )
        without_role = compile_scopes(
            [
                {"match": dict(match), "delivery": {"mode": ["all"]}}
                for match in written
            ],
            people=people,
            roles=roles,
            warn=warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert len(with_role) == len(without_role) == len(written)

    # The only fields resolution may write: the ids a role folds into, and the
    # names it keeps beside them for the description.
    folded = {"users", "roles"}
    for resolved, plain in zip(with_role, without_role):
        for field in fields(ScopeMatch):
            if field.name in folded:
                continue
            assert getattr(resolved.match, field.name) == getattr(
                plain.match, field.name
            ), f"role resolution dropped {field.name}"

    named = {
        field.name
        for field in fields(ScopeMatch)
        if any(getattr(scope.match, field.name) is not None for scope in with_role)
    }
    assert {field.name for field in fields(ScopeMatch)} - named == {
        "not_users",
        "not_roles",
    }

    # The chat-type restriction remains part of the composed rule.
    assert with_role[1].match.chat_type == ("direct",)
    assert with_role[1].selects(
        channel="every", chat="D1", chat_type="direct", user="U_BOSS"
    )
    assert not with_role[1].selects(
        channel="every", chat="C9", chat_type="room", user="U_BOSS"
    )
