"""Skill scope validation and restoration require no harness."""

import pytest

from belle_agent.scopes.skills import (
    bind_scoped_skills,
    reset_scoped_skills,
    scoped_skill_names,
    unrestricted_skill_inventory,
)


def test_scope_binding_restores_outer_scope() -> None:
    outer = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        assert scoped_skill_names() == frozenset({"ambient"})
        inner = bind_scoped_skills({"agent_skills_available": []})
        try:
            assert scoped_skill_names() == frozenset()
        finally:
            reset_scoped_skills(inner)
        with unrestricted_skill_inventory():
            assert scoped_skill_names() is None
        assert scoped_skill_names() == frozenset({"ambient"})
    finally:
        reset_scoped_skills(outer)
    assert scoped_skill_names() is None


def test_required_skill_must_be_available() -> None:
    with pytest.raises(ValueError, match="outside agent_skills_available"):
        bind_scoped_skills({"agent_skills_available": [], "agent_skills_required": ["ambient"]})


def test_explicit_null_catalog_does_not_inherit_all() -> None:
    with pytest.raises(ValueError, match="agent_skills_available must be a list"):
        bind_scoped_skills({"agent_skills_available": None})


def test_unavailable_skills_leave_the_catalog_or_the_installed_skills() -> None:
    tokens = bind_scoped_skills({"agent_skills_unavailable": ["b"]}, lambda: ["a", "b", "c"])
    try:
        assert scoped_skill_names() == frozenset({"a", "c"})
        with unrestricted_skill_inventory():
            assert scoped_skill_names() is None
        inner = bind_scoped_skills({"agent_skills_available": ["a", "b"], "agent_skills_unavailable": ["b"]})
        try:
            assert scoped_skill_names() == frozenset({"a"})
        finally:
            reset_scoped_skills(inner)
    finally:
        reset_scoped_skills(tokens)
    assert scoped_skill_names() is None


def test_unavailable_skills_without_the_installed_names_are_refused() -> None:
    tokens = bind_scoped_skills({"agent_skills_unavailable": ["b"]})
    try:
        with pytest.raises(ValueError, match="agent_skills_unavailable needs the names of the installed Skills"):
            scoped_skill_names()
    finally:
        reset_scoped_skills(tokens)


def test_a_required_skill_must_not_be_unavailable() -> None:
    with pytest.raises(ValueError, match="agent_skills_required and agent_skills_unavailable both name: b"):
        bind_scoped_skills({"agent_skills_unavailable": ["b"], "agent_skills_required": ["a", "b"]})
    with pytest.raises(ValueError, match="agent_skills_unavailable must be a list of Skill names"):
        bind_scoped_skills({"agent_skills_unavailable": "b"})
