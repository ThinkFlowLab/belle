"""The shared grammar of a chat command, and a platform with no model menu."""

from belle_agent.commands import grammar as command_grammar
from belle_agent.commands.grammar import MODELS_LIST, MODELS_USE, Subcommand, resolve
from belle_agent.models import ConfiguredModels

HELP = Subcommand(("help",), "List the commands.")
TABLE = {sub.words: sub for sub in (HELP, MODELS_LIST, MODELS_USE)}


def test_a_command_names_the_longest_subcommand_and_keeps_the_rest():
    assert resolve("Models USE  fast one", TABLE, HELP) == (MODELS_USE, "fast one")
    assert resolve("  ", TABLE, HELP) == (HELP, "")
    assert resolve("nothing here", TABLE, HELP) == (None, "")


async def test_with_no_menu_a_model_use_with_no_name_gets_its_usage_and_changes_nothing():
    said, choices = [], {}

    async def answer(text, **_):
        said.append(text)

    outcome = await command_grammar.run_models_command(
        MODELS_USE, "", models=ConfiguredModels(names=("m",)), scoped="", overrides=choices,
        key=("w", "c"), command="/belle", answer=answer, answer_lines=None, picker_blocks=None, escape=str,
    )

    assert outcome == "refused:models-name-required"
    assert said == ["Usage: `/belle models use <name>`"] and choices == {}


def test_only_an_angle_bracketed_part_is_a_required_argument():
    def required(arguments):
        return Subcommand(("x",), "", arguments).requires_arguments

    assert not required("") and not required("[cursor]") and not required("[message link]")
    assert required("<name>") and required("<session id> [cursor]") and required("<session id> [run id] [cursor]")
