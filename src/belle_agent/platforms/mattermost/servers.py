"""Several Mattermost servers behind one platform of the conversation host.

``platforms.mattermost.servers`` lists servers, each with a name, an
address, a bot token and an action listener of its own. This platform holds
one connector per entry: one WebSocket, one REST client, one listener and one
set of toolkits. Nothing of one server is reachable from another:

- The workspace of a record starts with the name of the server it came from,
  then a colon, then the team id or ``direct``. A team id names a team of one
  server only.
- Every action the host asks for names a workspace, and is performed by the
  connector of that server or not at all.
- A tool call runs on the toolkits of the server its turn came from. Those
  hold that server's token and no other, so an id a turn names is looked up
  on its own server only.
- A click and a slash command reach the listener of the server that sent
  them, at the address that server was given, and are checked against that
  listener's secret and that server's command token.
"""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.installations import Installations
from belle_agent.platforms.mattermost.client import MattermostError
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import KEY_PATH, KEY_SERVERS, read_servers
from belle_agent.platforms.mattermost.toolkit import METADATA_SERVER, _refused

logger = logging.getLogger(__name__)

#: The refusal of a tool call whose turn names no server this process serves.
SERVER_UNKNOWN = "mattermost_server_unknown"


def server_of(workspace: str) -> str:
    """The name of the server a workspace belongs to, or empty where it names none."""
    name, colon, _place = workspace.partition(":")
    return name if colon else ""


def _unknown_tool(name: str, server: str) -> str | None:
    if name == "widening_question":
        return None
    return _refused(
        SERVER_UNKNOWN,
        f"this turn came from the Mattermost server {server or '(none)'}, which this deployment does not"
        " serve now, so no Mattermost tool can act for it",
    )


class MattermostServers(Installations):
    """The platform port over one ``MattermostPlatform`` per server."""

    @property
    def workspaces(self) -> None:
        """Team IDs are validated by the selected server when used."""
        return None

    def __init__(self, members: list[MattermostPlatform], *, started_elsewhere: bool = False) -> None:
        super().__init__(
            {member.server: member for member in members},
            noun="server",
            where=f"{KEY_PATH}.{KEY_SERVERS}",
            installation_of=server_of,
            metadata_key=METADATA_SERVER,
            unknown_tool=_unknown_tool,
            logger=logger,
            started_elsewhere=started_elsewhere,
        )
        #: The id of each server's installation, once started.
        self._installations: dict[str, str] = {}

    async def refuse_twice_served(self, name: str, member: Any) -> None:
        """Refuse a server that another entry already serves: a message there could have two turns.

        The same bot, or the same ``DiagnosticId``, which the server gives its installation, names one server.
        """
        try:
            installation = str((await member.directory.client_config()).get("DiagnosticId") or "")
        except MattermostError:
            installation = ""
        for other in self.serving.values():
            if other.bot_user_id == member.bot_user_id:
                same = "its bot is the bot of"
            elif installation and installation == self._installations[other.server]:
                same = "its server has the installation id of"
            else:
                continue
            raise BelleConfigError(
                f"{same} server {other.server}, so both entries name one server. Each would answer the same message"
            )
        self._installations[name] = installation


def create_servers(config: Mapping[str, Any]) -> MattermostServers:
    """Build one connector per entry of ``servers`` that is on. None is connected."""
    members = [MattermostPlatform(settings) for settings in read_servers(config)]
    if not members:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{KEY_SERVERS} holds no server that is on")
    return MattermostServers(members)
