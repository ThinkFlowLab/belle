"""Platform-neutral addresses for scheduled delivery."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from belle_agent.scheduling.refusals import Refused

_FIELDS = ("platform", "workspace", "conversation", "thread")


@dataclass(frozen=True)
class Address:
    """One platform conversation and optional thread."""

    platform: str
    workspace: str
    conversation: str
    thread: str

    def json(self) -> dict[str, str]:
        """Return the stored address with stable field order."""
        return {key: getattr(self, key) for key in _FIELDS}


def address_from_json(value: Any) -> Address:
    """Read one complete stored address without guessing a missing field."""
    if not isinstance(value, Mapping) or set(value) != set(_FIELDS):
        raise Refused("address_invalid", "address must contain platform, workspace, conversation and thread")
    if any(not isinstance(value[key], str) for key in _FIELDS) or not value["conversation"]:
        raise Refused("address_invalid", "address fields must be strings and conversation must be nonempty")
    return Address(value["platform"], value["workspace"], value["conversation"], value["thread"])


async def expand_deliver_to(
    value: Any,
    origin: Address,
    *,
    open_dm: Callable[[], Awaitable[Address]] | None = None,
) -> Address:
    """Expand a delivery shorthand or partial address from the origin."""
    if value is None or value == "thread":
        return replace(origin, thread=origin.thread or "new")
    if value == "conversation":
        return replace(origin, thread="")
    if value == "creator_dm":
        if open_dm is None:
            raise Refused("delivery_unavailable", "deliver_to creator_dm needs a platform that can open a direct message")
        address = await open_dm()
        if not isinstance(address, Address) or not address.conversation:
            raise Refused("delivery_unavailable", "deliver_to creator_dm needs a complete direct address")
        return address
    if not isinstance(value, Mapping):
        raise Refused("address_invalid", "deliver_to must be thread, conversation, creator_dm or an address object")
    unknown = set(value) - set(_FIELDS)
    if unknown:
        raise Refused("invalid_arguments", f"deliver_to has unknown fields: {', '.join(sorted(map(str, unknown)))}")
    if any(not isinstance(field, str) for field in value.values()):
        raise Refused("address_invalid", "deliver_to fields must be strings")
    platform = value.get("platform", origin.platform)
    workspace = value.get("workspace", "" if platform != origin.platform else origin.workspace)
    conversation = value.get("conversation", origin.conversation)
    thread = value.get("thread", "" if conversation != origin.conversation else origin.thread)
    if not conversation:
        raise Refused("address_invalid", "deliver_to.conversation must be a nonempty string")
    return Address(platform, workspace, conversation, thread)


def validate_address(address: Address, platforms: Mapping[str, frozenset[str] | None]) -> None:
    """Check the named connection and workspace against the host's available places."""
    if address.platform not in platforms:
        names = ", ".join(sorted(platforms)) or "none"
        raise Refused("address_invalid", f"deliver_to.platform must name one of: {names}")
    workspaces = platforms[address.platform]
    if workspaces is not None and address.workspace not in (workspaces or {""}):
        names = ", ".join(sorted(workspaces)) or "the unnamed workspace (empty string)"
        raise Refused("address_invalid", f"deliver_to.workspace for {address.platform} must name one of: {names}")
    if not address.conversation:
        raise Refused("address_invalid", "deliver_to.conversation must be a nonempty string")
