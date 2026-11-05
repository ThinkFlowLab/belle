"""The shared name lookup: how a name matches, which matches survive the cap, and what coverage says."""

from belle_agent.tools.lookup import MAX_MATCHES, lookup_kinds, lookup_query, lookup_result, matches_in

KINDS = ("person", "chat")


def test_each_match_says_how_and_on_which_field_it_matched():
    people = [
        {"kind": "person", "id": "1", "name": "ada", "real_name": "Ada Lovelace", "is_active": True},
        {"kind": "person", "id": "2", "name": "adam", "real_name": "Adam Smith", "is_active": True},
        {"kind": "person", "id": "3", "name": "grace", "real_name": "Grace Ada", "is_active": False},
        {"kind": "person", "id": "4", "name": "bob", "real_name": "Bob", "is_active": True},
    ]

    found = matches_in(people, lookup_query("@Ada")[1], ("name", "real_name"))

    assert [(match["id"], match["matched"], match["match_type"]) for match in found] == [
        ("1", "name", "exact"), ("2", "name", "prefix"), ("3", "real_name", "contains")
    ]


def test_a_capped_result_keeps_the_best_matches_and_reports_the_true_count_and_what_was_not_searched():
    chats = [{"kind": "chat", "id": str(index), "name": f"ops-{index:02d}", "is_active": True} for index in range(25)]
    chats.append({"kind": "chat", "id": "x", "name": "ops", "is_active": True})

    result = lookup_result(
        "ops", KINDS, KINDS, matches_in(chats, "ops", None), {"chat": {}}, {"person": {"error": "forbidden"}}, [],
        unavailable_note="people could not be listed",
    )

    assert (result["match_count"], result["returned"], result["truncated"]) == (26, MAX_MATCHES, True)
    assert result["matches"][0]["id"] == "x"
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["kinds_searched"] == ["chat"] and result["coverage"]["kinds_unavailable"] == ["person"]
    assert result["coverage"]["unavailable_note"] == "people could not be listed"


def test_no_kind_means_every_kind_and_an_unknown_kind_is_named():
    assert lookup_kinds(None, KINDS) == (KINDS, [])
    assert lookup_kinds(["chat", "planet"], KINDS) == (("chat",), ["planet"])
