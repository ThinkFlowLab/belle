"""Compile people and roles into platform identities."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from belle_agent.scopes.capabilities import Warn, freeze_nested, text_keys

PEOPLE_KEY = "people"
ROLES_KEY = "roles"


@dataclass(frozen=True)
class PeopleDirectory:
    """Platform IDs for people and their role memberships."""

    people: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "people", freeze_nested(self.people))
        object.__setattr__(self, "roles", MappingProxyType({str(k): tuple(v) for k, v in self.roles.items()}))

    def knows_role(self, role: str) -> bool:
        """Distinguish undeclared roles from roles with no matching IDs."""
        return role in self.roles

    def members(self, role: str) -> tuple[str, ...]:
        return self.roles.get(role, ())

    def ids_for_role(self, role: str, *, channel: str | None) -> tuple[str, ...]:
        """Return sorted IDs for the role on one channel."""
        if not channel:
            return ()
        found: set[str] = set()
        for person in self.members(role):
            found.update(self.people.get(person, {}).get(channel, ()))
        return tuple(sorted(found))

    def channels_for_role(self, role: str) -> tuple[str, ...]:
        """Return platforms where any role member has an ID."""
        found: set[str] = set()
        for person in self.members(role):
            found.update(self.people.get(person, {}))
        return tuple(sorted(found))


EMPTY_DIRECTORY = PeopleDirectory()


def _name(raw: Any) -> str | None:
    """Return a stripped name, or ``None`` if it is not text or is empty."""
    return (raw.strip() or None) if isinstance(raw, str) else None


def _compile_person_ids(raw: Any, *, label: str, refuse: Warn) -> tuple[str, ...] | None:
    """Accept one ID or a list of IDs for one person on one platform."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    ids: list[str] = []
    for item in values:
        if not isinstance(item, str):
            refuse("%s=%r is not an id or a list of ids. Quote each id so that YAML keeps it as text", label, raw)
            return None
        if not item.strip():
            refuse("%s contains an empty id. Write each id as text", label)
            return None
        ids.append(item.strip())
    return tuple(sorted(set(ids)))


def _compile_people(raw: Any, *, refuse: Warn) -> dict[str, dict[str, tuple[str, ...]]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        refuse("%s=%r is not a mapping. Write each person as name: {platform: id}", PEOPLE_KEY, raw)
        return {}

    settled: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, raw_ids in text_keys(raw, label=PEOPLE_KEY, noun="name", refuse=refuse).items():
        if not isinstance(raw_ids, Mapping):
            refuse("%s.%s=%r is not a mapping. Write {platform: id}", PEOPLE_KEY, name, raw_ids)
            raw_ids = {}
        per_channel: dict[str, tuple[str, ...]] = {}
        label = f"{PEOPLE_KEY}.{name}"
        for channel, value in text_keys(raw_ids, label=label, noun="platform name", refuse=refuse).items():
            ids = _compile_person_ids(value, label=f"{label}.{channel}", refuse=refuse)
            if ids:
                per_channel[channel] = ids
        # Keep the person so that roles can tell a person with no ID from an unknown name.
        settled[name] = per_channel
    return settled


def _compile_roles(
    raw: Any, *, people: Mapping[str, Mapping[str, tuple[str, ...]]], refuse: Warn
) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        refuse("%s=%r is not a mapping. Write each role as name: [person, ...]", ROLES_KEY, raw)
        return {}

    declared = text_keys(raw, label=ROLES_KEY, noun="name", refuse=refuse)
    settled: dict[str, tuple[str, ...]] = {}
    for name, raw_members in declared.items():
        label = f"{ROLES_KEY}.{name}"
        if isinstance(raw_members, Mapping):
            refuse(
                "%s is a mapping. A role is a list of names from %s."
                " Put what the role can do in a scope that matches role: %s",
                label,
                PEOPLE_KEY,
                name,
            )
            continue
        if isinstance(raw_members, str):
            refuse("%s=%r is one name. Write a list of names: [%s]", label, raw_members, raw_members)
            continue
        if not isinstance(raw_members, (list, tuple)):
            refuse("%s=%r is not a list of names from %s", label, raw_members, PEOPLE_KEY)
            continue
        if not raw_members:
            # An empty role could mean everyone or nobody.
            refuse("%s is an empty list. Write at least one name from %s", label, PEOPLE_KEY)
            continue

        members: list[str] = []
        for item in raw_members:
            member = _name(item)
            if member is None:
                refuse("%s contains %r, which is not a name. Write each name as text", label, item)
            elif member in people:
                members.append(member)
                continue
            elif member in declared:
                refuse("%s contains the role %s. A role cannot contain a role. Write the names of the people", label, member)
            else:
                refuse("%s contains %s, which is not in %s. Add %s to %s", label, member, PEOPLE_KEY, member, PEOPLE_KEY)
            break
        else:
            settled[name] = tuple(sorted(set(members)))
    return settled


def compile_people(people: Any = None, roles: Any = None) -> PeopleDirectory:
    """Compile top-level people and roles.

    Raise ``BelleConfigError`` that names every invalid entry: a dropped id or
    member would change who a role restriction applies to.
    """
    # Imported here: the config module needs yaml, and the control protocol imports scopes without it.
    from belle_agent.config import BelleConfigError

    if people is None and roles is None:
        return EMPTY_DIRECTORY
    problems: list[str] = []

    def refuse(message: str, *args: Any) -> None:
        problems.append(message % args)

    settled_people = _compile_people(people, refuse=refuse)
    settled_roles = _compile_roles(roles, people=settled_people, refuse=refuse)
    if problems:
        raise BelleConfigError("Belle config people and roles are not valid. Correct these entries:\n" + "\n".join(problems))
    return PeopleDirectory(people=settled_people, roles=settled_roles)
