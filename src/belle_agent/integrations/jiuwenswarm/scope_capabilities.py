"""Register Slack's scope capabilities with the model-name check JiuwenSwarm supplies.

The declaration and the validators of Slack's own vocabularies are in
``belle_agent.platforms.slack.scope_capabilities``. Which model names exist is
JiuwenSwarm's knowledge, so that one check stays here.
"""

from typing import Any

from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.platforms.slack.events import EVENT_TURN_FAMILIES  # noqa: F401 - kept as a name of this module
from belle_agent.platforms.slack.scope_capabilities import (  # noqa: F401 - the Slack half, kept importable from here
    _caution_events,
    _check_events,
    _check_mode,
    slack_capabilities,
)
from belle_agent.scopes import register_channel


def _check_model_name(value: Any) -> "str | None":
    """Whether ``model_name`` is one of the models actually configured.

    Checked rather than passed through because ``_resolve_model_by_name`` returns
    the default model for any name it does not have, and logs nothing. An
    unchecked typo runs that conversation on a model nobody chose, with nothing
    in the log to say so.

    Nothing to check against is not a reason to refuse: a config read that fails
    would otherwise refuse a correct setting, and the operator would have no way
    to see why.
    """
    known = belle_config.configured_models()
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        return f"={value!r} is not a model name"
    name = value.strip()
    if not name:
        return "is empty. Write a model name from models.defaults"
    if not known.known:
        return None
    if not known.accepts(name):
        return f"={value!r} is not one of the models configured in models.defaults ({known.describe()})"
    return None


SLACK_CAPABILITIES = slack_capabilities(check_model_name=_check_model_name)

register_channel(SLACK_CAPABILITIES)
