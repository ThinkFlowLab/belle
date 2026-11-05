"""Several Slack workspaces behind one platform of the conversation host.

``slack.workspaces`` lists installations. Each entry is one Slack app
installed in one workspace, with the two tokens of that app. This platform
holds one connector per entry: one Socket Mode connection, one web client,
one set of toolkits. Nothing of one workspace is reachable from another:

- A record is given the workspace of the connection it arrived on.
- Every action the host asks for names a workspace, and is performed by the
  connector of that workspace or not at all.
- A tool call runs on the toolkits of the workspace its turn came from. Those
  hold that workspace's token and no other, so a conversation id a turn
  names is looked up in its own workspace only.

The bot users of the other apps are bots to each app, so their marks in a channel the workspaces share
start no event turn. A reaction names no bot flag, and ``users.info`` tells the bots of any other app.

The entries must be different Slack apps. An app-level token is shared
across connections of one app: two entries with the tokens of one app would
each receive part of that app's events and miss the rest.

Each token is a ``${NAME}`` reference, read from the environment.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import OnInbound
from belle_agent.platforms.installations import Installations
from belle_agent.platforms.slack.config import normalize_slack_conf
from belle_agent.platforms.slack.connector import _SECRETS, NAME, SlackPlatform
from belle_agent.platforms.slack.history_policy import KEY_WORKSPACES, METADATA_TEAM_KEY

logger = logging.getLogger(__name__)


def _unknown_tool(name: str, team: str) -> str | None:
    logger.warning("slack tool %s refused: its turn names workspace %s, which is not served", name, team or "-")
    if name == "widening_question":
        return None
    return json.dumps({"ok": False, "error": "slack_workspace_unknown", "detail": "Slack workspace not served"})


class SlackWorkspaces(Installations):
    """The platform port over one ``SlackPlatform`` per entry of ``slack.workspaces``, by the entry's label.

    Records and actions name a workspace by its team id, which ``auth.test`` gives at start. An action can
    name only a workspace that has connected.
    """

    def __init__(self, members: list[SlackPlatform]) -> None:
        super().__init__(
            {member.label: member for member in members},
            noun="workspace",
            where=f"{NAME}.{KEY_WORKSPACES}",
            installation_of=self._serving_team,
            metadata_key=METADATA_TEAM_KEY,
            unknown_tool=_unknown_tool,
            logger=logger,
        )
        for member in members:
            member.other_apps = lambda member=member: frozenset(
                other.bot_user_id for other in members if other is not member and other.bot_user_id
            )

    def _serving_team(self, team: str) -> str:
        """The entry that serves workspace ``team``, or empty."""
        return next((name for name, member in self.serving.items() if team and member.team_id == team), "")

    installation_of_turn = _serving_team

    def inbound_of(self, member: Any, on_inbound: OnInbound) -> OnInbound:
        async def receive(record: Any) -> str:
            # The connection a record arrived on says which workspace it belongs to. Its payload does not decide.
            return await on_inbound(replace(record, workspace=member.team_id))

        return receive

    async def refuse_twice_served(self, name: str, member: Any) -> None:
        """Refuse an app installed in a workspace that another entry serves: both would answer the same message."""
        for other in self.serving.values():
            if other.team_id == member.team_id:
                raise BelleConfigError(
                    f"it is installed in workspace {member.team_id}, which {other.label} already serves."
                    " Two apps in one workspace would each answer the same message"
                )

    @property
    def workspaces(self) -> frozenset[str]:
        return frozenset(member.team_id for member in self.serving.values())


def create_workspaces(section: Mapping[str, Any]) -> SlackWorkspaces:
    """Build one connector per entry of ``slack.workspaces``. None is connected."""
    shared = {key: value for key, value in section.items() if key != KEY_WORKSPACES}
    members: list[SlackPlatform] = []
    seen: dict[str, str] = {}
    for index, block in enumerate(normalize_slack_conf(section)[KEY_WORKSPACES]):
        label = f"platforms.{NAME}.{KEY_WORKSPACES}[{index}]"
        if block.get("enabled") is False:
            logger.info("%s is disabled and is not connected", label)
            continue
        # The parser fills an entry's default channel from the connector-wide one, which is named elsewhere.
        if section[KEY_WORKSPACES][index].get("default_channel_id"):
            logger.warning("Belle config %s.default_channel_id is set, and this connector does not act on it", label)
        tokens, problem = {}, ""
        for key, (_single, prefix) in _SECRETS.items():
            value = tokens[key] = str(block.get(key) or "")
            if value in seen:
                raise BelleConfigError(
                    f"Belle config {label}.{key} is the token of {seen[value]} as well. Each entry is a Slack app of"
                    " its own: two connections on one app's tokens each receive part of its events and miss the rest"
                )
            if not value:
                problem = problem or f"its {key} is empty"
            elif not value.startswith(prefix):
                problem = problem or f"its {key} does not hold a token that starts with {prefix}"
        if problem:
            # As the JiuwenSwarm channel does for an entry with no credentials: the others are served.
            logger.error("%s is NOT SERVED: %s", label, problem)
            continue
        seen.update({value: label for value in tokens.values()})
        members.append(
            SlackPlatform(
                shared, label=label, bot_token_source=f"{label}.bot_token", app_token_source=f"{label}.app_token", **tokens
            )
        )
    if not members:
        raise BelleConfigError(f"Belle config platforms.{NAME}.{KEY_WORKSPACES} is set and holds no workspace that can be connected")
    return SlackWorkspaces(members)
