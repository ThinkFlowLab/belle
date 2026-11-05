"""Several Matrix accounts behind one platform of the conversation host.

``platforms.matrix.accounts`` lists accounts, each with a name, a homeserver, a user id, a store and secrets
of its own. This platform holds one connector per entry, each with one helper process. Nothing of one account
is reachable from another:

- The workspace of each conversation is the name of the account it came to.
- Every action the host asks for names a workspace, and the connector of that account performs it, or nothing
  is done.
- A tool call runs on the toolkit of the account its turn came to. That toolkit reads and writes with the
  helper of that account only.

The other accounts are bots to each account, as ``bot_user_ids`` are: their marks start no turn, and their ⏹️
stops none.
"""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.installations import Installations
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import KEY_ACCOUNTS, KEY_PATH, read_accounts
from belle_agent.platforms.matrix.toolkit import METADATA_ACCOUNT, _refused

logger = logging.getLogger(__name__)

#: The refusal of a tool call whose turn names no account this process serves.
ACCOUNT_UNKNOWN = "matrix_account_unknown"


def _unknown_tool(name: str, account: str) -> str | None:
    if name == "widening_question":
        return None
    return _refused(
        ACCOUNT_UNKNOWN,
        f"this turn came to the Matrix account {account or '(none)'}, which this deployment does not serve now,"
        " so no Matrix tool can act for it",
    )


class MatrixAccounts(Installations):
    """The platform port over one ``MatrixPlatform`` per account."""

    @property
    def workspaces(self) -> frozenset[str]:
        return frozenset(self.serving)

    def __init__(self, members: list[MatrixPlatform]) -> None:
        super().__init__(
            {member.workspace: member for member in members},
            noun="account",
            where=f"{KEY_PATH}.{KEY_ACCOUNTS}",
            # The workspace is the account's name (D9).
            installation_of=lambda workspace: workspace,
            metadata_key=METADATA_ACCOUNT,
            unknown_tool=_unknown_tool,
            logger=logger,
        )
        for member in members:
            member.other_accounts = lambda member=member: frozenset(
                user for other in members if other is not member for user in other.account_user_ids
            )

    async def refuse_twice_served(self, name: str, member: MatrixPlatform) -> None:
        """Refuse an account that another entry already serves: both would answer the same message.

        The homeserver says which account an entry logged in to. A user ID written in another case, for example,
        can log in to an account that another entry serves.
        """
        for other_name, other in self.serving.items():
            if other.user_id == member.user_id:
                device = await member.withdraw()
                raise BelleConfigError(
                    f"it logged in as {member.user_id}, which account {other_name} already serves, so both entries"
                    f" name one account and each would answer the same message. Remove one of them; {device}"
                )


def create_accounts(config: Mapping[str, Any], state_dir: Path | None = None) -> MatrixAccounts:
    """Build one connector per entry of ``accounts`` that is on. None is started."""
    return MatrixAccounts([MatrixPlatform(settings) for settings in read_accounts(config, state_dir)])
