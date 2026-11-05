"""Cron mutation warnings stay in Belle."""

from types import SimpleNamespace
from unittest.mock import patch

from belle_agent.integrations.jiuwenswarm import slack_routing


def test_mutation_warns_for_unproven_slack_session() -> None:
    job = SimpleNamespace(id="job-1", targets="slack", session_id="slack_T1_C1_U1", origin_channel_id="")
    with (
        patch.object(slack_routing.logger, "warning") as warning,
        patch.object(slack_routing, "warn_if_slack_cron_delivery_unreachable") as reachability,
    ):
        slack_routing.slack_cron_mutation(job, action="create", request_channel_id="web")
    assert "will not be given Slack history access" in warning.call_args.args[0]
    reachability.assert_called_once_with(targets="slack", session_id=job.session_id, job_id=job.id)


def test_mutation_keeps_proven_session() -> None:
    job = SimpleNamespace(id="job-1", targets="slack", session_id="slack_T1_C1_U1", origin_channel_id="slack")
    with (
        patch.object(slack_routing.logger, "warning") as warning,
        patch.object(slack_routing, "warn_if_slack_cron_delivery_unreachable") as reachability,
    ):
        slack_routing.slack_cron_mutation(job, action="update", request_channel_id="web")
    warning.assert_not_called()
    reachability.assert_called_once()
