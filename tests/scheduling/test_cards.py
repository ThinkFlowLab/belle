"""Schedule tool cards match the approved contract and deployment settings."""

import json
from pathlib import Path

from belle_agent.scheduling.cards import schedule_cards
from belle_agent.scheduling.settings import SchedulingSettings


def test_cards_equal_the_approved_contract() -> None:
    approved = json.loads(Path(__file__).with_name("cards.json").read_text())
    assert schedule_cards(SchedulingSettings()) == approved


def test_cards_follow_the_settings() -> None:
    settings = SchedulingSettings(
        min_interval_seconds=600,
        default_zone="Europe/Paris",
        job_visibility="all",
        delivery=[{"to": {"platform": "matrix", "workspace": "home"}}],
    )
    cards = {card["name"]: card for card in schedule_cards(settings)}
    create = cards["schedule_create"]["input_params"]["properties"]
    assert "at least 600 s apart" in create["when"]["description"]
    assert "zone defaults to Europe/Paris" in create["when"]["description"]
    assert create["when"]["properties"]["seconds"]["minimum"] == 600
    assert "platform matrix, workspace home" in create["deliver_to"]["description"]
    for name in ("schedule_list", "schedule_runs"):
        assert "all_conversations" in cards[name]["input_params"]["properties"]
        assert "of all conversations" in cards[name]["description"]
