"""Tool cards for the deployment's scheduler settings."""

import json
from importlib.resources import files
from typing import Any

from belle_agent.scheduling.settings import SchedulingSettings


def schedule_cards(settings: SchedulingSettings) -> list[dict[str, Any]]:
    """Return the six approved cards with their operator-controlled limits."""
    cards: list[dict[str, Any]] = json.loads(files("belle_agent.scheduling").joinpath("cards.json").read_text())
    by_name = {card["name"]: card for card in cards}
    create = by_name["schedule_create"]["input_params"]["properties"]
    when = create["when"]
    when["description"] = when["description"].replace("300 s", f"{settings.min_interval_seconds} s")
    when["properties"]["seconds"]["minimum"] = settings.min_interval_seconds
    if settings.default_zone:
        when["description"] = when["description"].replace(
            "It is required for cron and for each wall time. Never guess a zone: ask.",
            f"zone defaults to {settings.default_zone}.",
        )
    if settings.delivery != [{"to": {"same": ["platform", "workspace"]}}]:
        destinations = []
        for rule in settings.delivery:
            target = rule["to"]
            names = [f"{field} {target[field]}" for field in ("platform", "workspace") if field in target]
            if names:
                destinations.append(", ".join(names))
        if destinations:
            create["deliver_to"]["description"] += " Delivery rules also name " + ". ".join(destinations) + "."
    if settings.job_visibility == "all":
        for name in ("schedule_list", "schedule_runs"):
            card = by_name[name]
            card["description"] = card["description"].replace(
                "of this conversation", "of this conversation, or of all conversations", 1
            )
            card["input_params"]["properties"]["all_conversations"] = {
                "type": "boolean", "default": False,
                "description": "Show jobs of every conversation in this deployment.",
            }
    return cards
