"""Model-input validation stays usable without a platform SDK."""

import asyncio
import json
import re

from belle_agent.tools.inputs import checks_ids, enum_values, id_error, iso_instant, iso_utc, object_list, string_ids

KINDS = {"room_id": "room", "room_ids": "room"}
FORMS = {"room": (re.compile(r"\AR\d+\Z"), "an R followed by digits, such as R12")}


def test_enum_values_normalizes_deduplicates_and_reports_unknowns() -> None:
    assert enum_values([" READ ", "read", "", "owner"], ("read", "write")) == (["read"], ["owner"])
    assert enum_values("WRITE", ("write",)) == (["write"], [])
    assert enum_values(None, ()) == ([], [])
    assert enum_values(12, ()) == ([], ["12"])


def test_string_ids_preserves_case_order_and_duplicates() -> None:
    assert string_ids((" U1 ", "", "U1")) == ["U1", "U1"]
    assert string_ids(None) == []
    for invalid in ("U1", b"U1", ["U1", 1], {"U1": True}):
        assert string_ids(invalid) is None


def test_object_list_copies_mappings_and_rejects_malformed_collections() -> None:
    item = {"text": "hello"}
    copied = object_list((item,))
    assert copied == [item]
    assert copied[0] is not item
    assert object_list(None) == []
    for invalid in ("text", b"text", [item, 1], item):
        assert object_list(invalid) is None


def test_an_id_argument_is_checked_against_the_form_of_its_kind() -> None:
    assert id_error({"room_id": "R12", "room_ids": ["R1"], "text": "null"}, KINDS, FORMS, "Test") is None
    assert id_error({"room_id": None, "room_ids": ""}, KINDS, FORMS, "Test") is None
    assert id_error({"room_ids": ["R1", ""]}, KINDS, FORMS, "Test") == (
        "room_id_malformed",
        "room_ids is not a Test room id. A Test room id is an R followed by digits, such as R12",
    )


def test_the_decorator_refuses_before_the_tool_runs() -> None:
    ran = []

    @checks_ids(KINDS, FORMS, "Test", lambda code, detail: json.dumps({"ok": False, "error": code}))
    async def tool(room_id: str) -> str:
        ran.append(room_id)
        return "ran"

    assert json.loads(asyncio.run(tool(room_id="null")))["error"] == "room_id_malformed"
    assert asyncio.run(tool("R1")) == "ran" and ran == ["R1"]


def test_an_iso_instant_reads_dates_and_zones_and_writes_back_in_utc():
    assert iso_instant("2026-08-31") == iso_instant("2026-08-31T00:00:00Z") == iso_instant("2026-08-31T02:00:00+02:00")
    assert iso_instant("yesterday") is None
    assert iso_utc(iso_instant("2026-08-31T02:00:00+02:00")) == "2026-08-31T00:00:00Z"
