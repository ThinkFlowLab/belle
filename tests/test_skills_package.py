"""Packaged skills have no OpenJiuwen import requirement."""

import subprocess
import sys


def test_skills_are_available_without_openjiuwen():
    script = """
import sys
from importlib.resources import files

root = files("belle_agent.skills")
assert len([item for item in root.iterdir() if item.joinpath("SKILL.md").is_file()]) == 12
assert "openjiuwen" not in sys.modules
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
    )


def test_the_packaged_skills_are_listed_with_their_descriptions():
    from belle_agent.skills import packaged_skills

    listed = {skill["name"]: skill for skill in packaged_skills()}

    assert len(listed) == 12
    assert "session-work" in listed
    assert all(skill["installed"] is True and skill["description"] for skill in listed.values())
    assert listed["chat-digest"]["description"].startswith("Turn a span of conversation history into a digest")


def test_each_packaged_skill_states_its_description_on_one_line():
    # A harness that reads skill front matter line by line takes `>-` or `|` as the description.
    from importlib.resources import files

    for item in files("belle_agent.skills").iterdir():
        skill = item.joinpath("SKILL.md")
        if not skill.is_file():
            continue
        front = skill.read_text().split("---")[1]
        description = next(line for line in front.splitlines() if line.startswith("description:"))
        assert description.split(":", 1)[1].strip()[:1] not in {"", ">", "|"}, item.name
