"""Portable catalog policy preserves validation and name ambiguity."""

from belle_agent import capability_catalog as catalog


def test_skills_validate_before_deduplication_and_preserve_entries() -> None:
    enabled = {"name": " research ", "installed": True}
    entries = [None, {"name": "research", "installed": False}, enabled, enabled]
    assert catalog.usable_skills(entries, None) == [enabled]
    assert catalog.usable_skills(entries, []) == []
    assert catalog.usable_skills(entries, ["research"]) == [enabled]


def test_subagents_validate_trim_filter_and_preserve_first_occurrence() -> None:
    entries = [None, 12, "", " code ", "research", "code", "CODE"]
    assert catalog.usable_subagents(entries, None) == ["code", "research", "CODE"]
    assert catalog.usable_subagents(entries, ["code"]) == ["code"]
    assert catalog.usable_subagents(entries, []) == []


def test_name_resolution_prefers_exact_match_and_refuses_ambiguity() -> None:
    assert catalog.find_name(["code", "CODE"], "code") == "code"
    assert catalog.find_name(["code", "CODE"], "Code") == ""
    assert catalog.find_name(iter(["code"]), "CODE") == "code"
    assert catalog.find_skill([{"name": " code "}], "CODE") == "code"


def test_gloss_respects_clause_and_display_width() -> None:
    assert catalog.gloss("Extract text from PDFs. Use when asked to read a PDF.") == ("Extract text from PDFs")
    assert catalog.gloss("Agent 模板包的创建与更新器：从自然语言创建模板包。") == ("Agent 模板包的创建与更新器")
    shortened = catalog.gloss("识别代码中可替换为算子的算子" * 10)
    assert shortened.endswith("…")
    assert catalog._columns(shortened) <= catalog.GLOSS_COLUMNS



def test_catalog_filters_preserve_scope_and_installed_state() -> None:
    skills = [
        {"name": "research", "installed": True, "enabled": True},
        {"name": "ambient", "installed": True, "enabled": False},
        {"name": "remote", "installed": False, "enabled": True},
    ]
    assert [item["name"] for item in catalog.usable_skills(skills, None)] == ["research"]
    assert catalog.usable_skills(skills, ["ambient"]) == []
    assert catalog.usable_subagents(["research", "research", "code", ""], ["code"]) == ["code"]
    assert catalog.find_name(["code_agent"], "CODE_AGENT") == "code_agent"


def test_unavailable_skills_leave_the_list_whether_or_not_available_is_set() -> None:
    skills = [{"name": name, "installed": True} for name in ("notes", "digest", "help")]
    assert [item["name"] for item in catalog.usable_skills(skills, None, ["help"])] == ["notes", "digest"]
    assert [item["name"] for item in catalog.usable_skills(skills, ["notes", "help"], ["help"])] == ["notes"]
    assert catalog.scope_allows("notes", None) and not catalog.scope_allows("notes", None, {"notes"})
    assert not catalog.scope_allows("notes", set()) and catalog.scope_allows("notes", {"notes"}, {"help"})
