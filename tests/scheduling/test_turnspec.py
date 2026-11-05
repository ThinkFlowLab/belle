"""Scheduled turns stay within both places' capabilities."""

import pytest

from belle_agent.backend import BackendCapabilities, Catalog, TurnScope
from belle_agent.models import ConfiguredModels
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.turnspec import JobTurn, ScopeLimits, check_catalog, parse_turn, settle_run_scope

ORIGIN = Address("slack", "team", "origin", "")
TARGET = Address("slack", "team", "target", "")


def limits(place: Address, **scope: object) -> ScopeLimits:
    return ScopeLimits(place, TurnScope(**scope))


def test_parse_turn_rejects_unknown_and_wrongly_typed_fields() -> None:
    assert parse_turn({"model": "m", "skills": ["skill"], "subagents": []}) == JobTurn("m", ("skill",), "", ())
    for value in ({"other": 1}, {"skills": "skill"}, {"subagents": [""]}, {"model": None}, []):
        with pytest.raises(Refused) as error:
            parse_turn(value)
        assert error.value.code == "invalid_arguments"


def test_catalog_checks_names_and_required_capabilities() -> None:
    catalog = Catalog(
        models=ConfiguredModels(names=("model",), aliases=("alias",)),
        skills=({"name": "skill", "installed": True},),
        experts=("expert",),
        subagents=("agent",),
    )
    capabilities = BackendCapabilities(
        "test", scope_fields=frozenset({"skills_required", "expert", "subagents_available"})
    )
    check_catalog(
        JobTurn("alias", ("skill",), "expert", ("agent",)), catalog, capabilities,
        packaged=lambda name: name == "skill", supplies_skills=True,
    )
    for turn, code in (
        (JobTurn(model="missing"), "model_unknown"),
        (JobTurn(skills=("missing",)), "skill_unknown"),
        (JobTurn(expert="missing"), "expert_unknown"),
        (JobTurn(subagents=("missing",)), "subagent_unknown"),
    ):
        with pytest.raises(Refused) as error:
            check_catalog(turn, catalog, capabilities, packaged=lambda _: True, supplies_skills=False)
        assert error.value.code == code


def test_scope_takes_strictest_tools_and_refuses_questions() -> None:
    origin = limits(ORIGIN, tools={"write": "ask", "read": "allow"})
    target = limits(TARGET, tools={"write": "deny", "read": "ask"})
    scope = settle_run_scope(JobTurn(), origin, target, refuse_questions=True)
    assert scope.tools == {"write": "deny", "read": "deny"}
    assert not scope.withhold_final


def test_job_skill_outside_target_is_refused() -> None:
    origin = limits(ORIGIN)
    target = limits(TARGET, skills_available=frozenset({"other"}))
    with pytest.raises(Refused) as error:
        settle_run_scope(JobTurn(skills=("needed",)), origin, target, refuse_questions=False)
    assert error.value.code == "turn_out_of_scope"
    assert "target" in error.value.detail


def test_required_skills_are_ordered_and_subagents_intersect() -> None:
    origin = limits(
        ORIGIN, skills_required=("first", "shared"), subagents_available=frozenset({"a", "b"})
    )
    target = limits(
        TARGET, skills_required=("shared", "second"), subagents_available=frozenset({"b", "c"})
    )
    scope = settle_run_scope(
        JobTurn(skills=("first", "job"), subagents=("b",)), origin, target,
        refuse_questions=False,
    )
    assert scope.skills_required == ("first", "shared", "second", "job")
    assert scope.subagents_available == frozenset({"b"})


def test_scope_models_must_agree_even_when_job_does_not_select_one() -> None:
    origin = limits(ORIGIN, model="one")
    target = limits(TARGET, model="two")
    with pytest.raises(Refused) as error:
        settle_run_scope(JobTurn(), origin, target, refuse_questions=False)
    assert error.value.code == "turn_out_of_scope"
    assert "model" in error.value.detail
    assert settle_run_scope(
        JobTurn(model="one"), origin, limits(TARGET), refuse_questions=False
    ).model == "one"
