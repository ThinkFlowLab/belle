"""Delivery address parsing and connection checks."""

import pytest

from belle_agent.scheduling.address import Address, address_from_json, expand_deliver_to, validate_address
from belle_agent.scheduling.refusals import Refused

ORIGIN = Address("slack", "team-a", "channel-a", "root-a")


@pytest.mark.asyncio
async def test_shorthands_expand_from_the_origin() -> None:
    assert await expand_deliver_to(None, ORIGIN) == ORIGIN
    assert await expand_deliver_to("thread", Address("slack", "team-a", "channel-a", "")) == Address("slack", "team-a", "channel-a", "new")
    assert await expand_deliver_to("conversation", ORIGIN) == Address("slack", "team-a", "channel-a", "")

    async def open_dm() -> Address:
        return Address("slack", "direct", "dm-a", "")

    assert await expand_deliver_to("creator_dm", ORIGIN, open_dm=open_dm) == Address("slack", "direct", "dm-a", "")


@pytest.mark.asyncio
async def test_defaults_reset_thread_and_workspace() -> None:
    assert await expand_deliver_to({"conversation": "channel-b"}, ORIGIN) == Address("slack", "team-a", "channel-b", "")
    assert await expand_deliver_to({"platform": "matrix"}, ORIGIN) == Address("matrix", "", "channel-a", "root-a")
    assert await expand_deliver_to({"conversation": "channel-b", "thread": "new"}, ORIGIN) == Address("slack", "team-a", "channel-b", "new")


@pytest.mark.asyncio
async def test_unknown_field_uses_invalid_arguments() -> None:
    with pytest.raises(Refused) as error:
        await expand_deliver_to({"room": "other"}, ORIGIN)
    assert error.value.code == "invalid_arguments" and "room" in error.value.detail
    with pytest.raises(Refused) as error:
        await expand_deliver_to({"conversation": 3}, ORIGIN)
    assert error.value.code == "address_invalid" and "strings" in error.value.detail


def test_address_invalid_lists_the_valid_values() -> None:
    platforms = {"slack": frozenset({"team-a", "team-b"}), "matrix": frozenset()}
    with pytest.raises(Refused, match="matrix, slack"):
        validate_address(Address("other", "", "room", ""), platforms)
    with pytest.raises(Refused, match="team-a, team-b"):
        validate_address(Address("slack", "", "room", ""), platforms)
    with pytest.raises(Refused, match="unnamed workspace"):
        validate_address(Address("matrix", "team-a", "room", ""), platforms)
    validate_address(Address("mattermost", "unseen-team", "room", ""), {"mattermost": None})


def test_full_address_keeps_field_order() -> None:
    assert list(address_from_json(ORIGIN.json()).json()) == ["platform", "workspace", "conversation", "thread"]
