"""The search, name and pin tools on the fake server: what each result holds, and what its coverage says."""

import inspect
import json

import pytest
from test_mattermost_tools import ALICE, BOB, CAROL, ELSEWHERE, LOUNGE, OUTSIDE, PAIR, QUIET, TEAM

from belle_agent.platforms.mattermost.directory import Directory, MembersIncomplete
from belle_agent.platforms.mattermost.reading import MattermostReadingToolkit, _Listing
from belle_agent.platforms.mattermost.toolkit import MattermostToolkit


async def test_directory_scan_budget_reaches_page_26() -> None:
    class PagedClient:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def request(self, method, path, *, params):
            page = params["page"]
            self.pages.append(page)
            return [{"id": str(page)}] * 200 if page < 25 else [{"id": "late-match"}]

    client = PagedClient()
    tool = MattermostReadingToolkit(
        client, None, lambda: "", dict, directory_scan_max_pages=26,
    )
    records, complete = await tool._pages("/users")
    assert complete and len(records) == 5001
    assert records[-1]["id"] == "late-match"
    assert client.pages == list(range(26))

    limited = MattermostReadingToolkit(client, None, lambda: "", dict, directory_scan_max_pages=25)
    _, complete = await limited._pages("/users")
    assert not complete


async def test_native_user_search_finds_a_match_beyond_a_partial_directory_scan() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def request(self, method, path, *, json):
            self.calls.append((method, path, json))
            return [{"id": "late-user", "username": "late_user"}]

    client = Client()
    toolkit = MattermostReadingToolkit(client, None, lambda: "", dict, directory_scan_max_pages=25)

    async def partial_listing(kind):
        return _Listing([], False)

    toolkit._listing = partial_listing
    result = json.loads(await toolkit.find_by_name("late_user", kind="person"))

    assert [match["id"] for match in result["matches"]] == ["late-user"]
    assert result["coverage"]["status"] == "partial"
    assert client.calls == [("POST", "/users/search", {"term": "late_user", "limit": 5000, "allow_inactive": True})]


async def test_name_lookup_can_return_more_than_the_default_limit() -> None:
    class Client:
        async def request(self, method, path, *, json):
            return [{"id": f"person-{index}", "username": f"late_user_{index}"} for index in range(21)]

    toolkit = MattermostReadingToolkit(Client(), None, lambda: "", dict)

    async def partial_listing(kind):
        return _Listing([], False)

    toolkit._listing = partial_listing
    result = json.loads(await toolkit.find_by_name("late_user", kind="person", max_matches=21))

    assert result["match_count"] == result["returned"] == 21
    assert result["truncated"] is False


async def test_name_lookup_rejects_invalid_result_limit() -> None:
    toolkit = MattermostReadingToolkit(None, None, lambda: "", dict)
    result = json.loads(await toolkit.find_by_name("late_user", max_matches=0))

    assert result["error"] == "max_matches_invalid"


async def test_native_channel_search_finds_a_public_channel_after_a_partial_scan() -> None:
    class Client:
        async def request(self, method, path, *, json):
            assert (method, path, json) == ("POST", f"/teams/{TEAM}/channels/search", {"term": "late"})
            return [{"id": "late-channel", "type": "O", "name": "late"}]

    toolkit = MattermostReadingToolkit(Client(), None, lambda: "", dict)

    async def partial_listing(kind):
        return _Listing([], False)

    async def team_ids():
        return [TEAM]

    toolkit._listing = partial_listing
    toolkit._team_ids = team_ids
    result = json.loads(await toolkit.find_by_name("late", kind="chat"))

    assert [match["id"] for match in result["matches"]] == ["late-channel"]
    assert result["coverage"]["status"] == "partial"


async def test_audience_member_page_budget_refuses_before_using_incomplete_members() -> None:
    class Client:
        async def request(self, method, path, *, params):
            page = params["page"]
            return [{"user_id": str(index)} for index in range(200)] if page == 0 else [{"user_id": "last"}]

    limited = Directory(Client(), audience_member_max_pages=1)
    with pytest.raises(MembersIncomplete, match="audience_member_max_pages"):
        await limited.members("channel")

    complete = Directory(Client(), audience_member_max_pages=2)
    assert "last" in await complete.members("channel")


def test_each_reading_card_requires_only_the_arguments_its_tool_cannot_do_without(turn):
    shared = {card["name"] for card in MattermostToolkit.get_tool_definitions(None)}
    for card in turn.tools.get_tool_definitions():
        if card["name"] in shared:
            continue
        parameters = inspect.signature(getattr(type(turn.tools), card["name"])).parameters.values()
        without_default = {parameter.name for parameter in parameters if parameter.default is inspect.Parameter.empty} - {"self"}
        assert set(card["input_params"].get("required", [])) == without_default
        assert set(card["input_params"]["properties"]) == {parameter.name for parameter in parameters} - {"self"}


# ── read_mattermost_pinned_messages ──────────────────────────────────────


async def test_the_pins_of_this_channel_are_listed_newest_first_and_another_channel_s_only_where_its_readers_may(turn):
    turn.came_from(PAIR)
    turn.rest.post("1" * 26, PAIR, ALICE, "first", is_pinned=True)
    turn.rest.post("2" * 26, PAIR, BOB, "not pinned")
    turn.rest.post("3" * 26, PAIR, ALICE, "second", is_pinned=True)

    pins = await turn.call("read_mattermost_pinned_messages")
    turn.came_from(LOUNGE, chat_type="O")
    widening = await turn.call("read_mattermost_pinned_messages", chat_id=PAIR)

    assert [message["text"] for message in pins["pinned_messages"]] == ["second", "first"]
    assert pins["coverage"] == {**pins["coverage"], "status": "complete", "pinned_messages_returned": 2}
    assert pins["pinned_messages"][0]["permalink"] == "http://chat.invalid/team-tttt/pl/" + "3" * 26
    assert widening["error"] == "source_is_public_and_target_is_not"


# ── search_messages ──────────────────────────────────────────────────────


async def test_a_search_names_the_open_channels_it_could_not_search(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.post("1" * 26, LOUNGE, ALICE, "the ferry is late")
    turn.rest.post("2" * 26, OUTSIDE, BOB, "the ferry left")

    found = await turn.call("search_messages", query="ferry")

    assert [(message["text"], message["chat_id"]) for message in found["messages"]] == [("the ferry is late", LOUNGE)]
    coverage = found["coverage"]
    assert coverage["channels"] == "partial" and coverage["partial_reasons"] == ["channels_not_searched"]
    assert coverage["channels_not_searched"] == [
        {"chat_id": ELSEWHERE, "chat_name": "Elsewhere"}, {"chat_id": OUTSIDE, "chat_name": "Outside"}
    ]
    assert coverage["older_matches"] == "not_reported_by_server" and "oldest_result_iso_utc" in coverage


async def test_a_search_that_finds_nothing_still_says_what_it_could_not_search(turn):
    turn.came_from(LOUNGE, chat_type="O")

    found = await turn.call("search_messages", query="walrus")

    assert found["messages"] == [] and found["coverage"]["older_matches"] == "none"
    assert found["coverage"]["channels_not_searched_count"] == 2


async def test_unsearched_channel_names_have_a_caller_controlled_result_limit(turn):
    turn.came_from(LOUNGE, chat_type="O")

    limited = await turn.call("search_messages", query="walrus", max_unsearched_channels=1)
    complete = await turn.call("search_messages", query="walrus", max_unsearched_channels=2)
    invalid = await turn.call("search_messages", query="walrus", max_unsearched_channels=0)

    assert limited["coverage"]["channels_not_searched_count"] == 2
    assert len(limited["coverage"]["channels_not_searched"]) == 1
    assert limited["coverage"]["channels_not_searched_truncated"] is True
    assert len(complete["coverage"]["channels_not_searched"]) == 2
    assert complete["coverage"]["channels_not_searched_truncated"] is False
    assert invalid["error"] == "max_unsearched_channels_invalid"


async def test_a_match_this_conversation_s_readers_cannot_read_is_withheld_and_counted_but_not_named(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.post("1" * 26, QUIET, ALICE, "budget draft")
    turn.rest.post("2" * 26, LOUNGE, BOB, "budget meeting")

    found = await turn.call("search_messages", query="budget")

    assert [message["chat_id"] for message in found["messages"]] == [LOUNGE]
    assert (found["coverage"]["results_withheld"], found["coverage"]["channels_withheld"]) == (1, 1)
    assert QUIET not in str(found["coverage"])


async def test_search_options_reach_server_and_paging_requires_server_support(turn):
    turn.came_from(LOUNGE, chat_type="O")
    for index in range(3):
        turn.rest.post(str(index + 1) * 26, LOUNGE, ALICE, f"ferry {index}")

    turn.rest.search_paging = True
    found = await turn.call(
        "search_messages", query="ferry", team_id=TEAM, page=0, per_page=2,
        is_or_search=True, time_zone_offset=60, include_archived_channels=True,
    )
    assert len(found["messages"]) == 2
    token = found["coverage"]["next_page_token"]
    assert token
    second = await turn.call(
        "search_messages", query="ferry", team_id=TEAM, page_token=token, per_page=2,
        is_or_search=True, time_zone_offset=60, include_archived_channels=True,
    )
    assert len(second["messages"]) == 1
    assert second["coverage"]["next_page_token"] is None
    assert (await turn.call("search_messages", query="other", team_id=TEAM, page_token=token))["error"] == "invalid_page_token"
    turn.came_from(LOUNGE, sender=BOB, chat_type="O")
    assert (await turn.call("search_messages", query="ferry", team_id=TEAM, page_token=token,
                            per_page=2, is_or_search=True, time_zone_offset=60,
                            include_archived_channels=True))["error"] == "invalid_page_token"
    turn.came_from(LOUNGE, chat_type="O")
    calls = turn.rest.calls("POST", f"/teams/{TEAM}/posts/search")
    assert calls[0].body == {
        "terms": "ferry", "is_or_search": True, "time_zone_offset": 60,
        "include_deleted_channels": True, "page": 0, "per_page": 2,
    }
    global_first = await turn.call("search_messages", query="ferry", page=0, per_page=2)
    global_second = await turn.call("search_messages", query="ferry", page_token=global_first["coverage"]["next_page_token"], per_page=2)
    assert len(global_first["messages"]) == 2 and len(global_second["messages"]) == 1
    assert turn.rest.calls("POST", "/posts/search")

    turn.rest.search_paging = False
    refused = await turn.call("search_messages", query="ferry", team_id=TEAM, page=0, per_page=2)
    assert refused["error"] == "search_paging_unavailable"


async def test_file_search_only_returns_downloadable_files_of_readable_channels(turn):
    turn.came_from(LOUNGE, chat_type="O")
    public = "f" * 26
    private = "s" * 26
    turn.rest.file(public, LOUNGE, "report.md", b"indexed note", post="1" * 26)
    turn.rest.file(private, QUIET, "secret-report.md", b"indexed note", post="2" * 26)

    found = await turn.call("search_mattermost_files", query="report")
    detail = await turn.call("download_file", file_id=found["files"][0]["file_id"])

    assert [item["file_id"] for item in found["files"]] == [public]
    assert (detail["file_id"], detail["content"]) == (public, "indexed note")
    assert private not in str(found)
    assert found["coverage"]["results_returned"] == 1
    assert turn.rest.calls("POST", f"/teams/{TEAM}/files/search")[0].body["terms"] == "report"
    assert turn.rest.calls("POST", f"/teams/{TEAM}/files/search")[0].headers["content-type"] == "application/json"


async def test_file_search_checks_each_channel_once(turn, monkeypatch):
    turn.came_from(PAIR, chat_type="P")
    turn.rest.file("f" * 26, QUIET, "report-one.md", b"one")
    turn.rest.file("g" * 26, QUIET, "report-two.md", b"two")
    checks = 0
    original = turn.tools._may_read

    async def checked(origin, target, requester):
        nonlocal checks
        checks += 1
        await original(origin, target, requester)

    monkeypatch.setattr(turn.tools, "_may_read", checked)
    found = await turn.call("search_mattermost_files", query="report")

    assert len(found["files"]) == 2
    assert checks == 1


async def test_file_search_refuses_paging_when_the_server_ignores_page(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.file("f" * 26, LOUNGE, "report.md", b"text")

    found = await turn.call("search_mattermost_files", query="report", team_id=TEAM, page=0)

    assert found["error"] == "search_paging_unavailable"


async def test_search_page_size_rejects_server_truncation(turn):
    turn.came_from(LOUNGE, chat_type="O")

    for tool in ("search_messages", "search_mattermost_files"):
        result = await turn.call(tool, query="report", page=0, per_page=201)
        assert result["error"] == "invalid_per_page"
        assert "1 to 200" in result["detail"]


async def test_searches_include_archived_channels_only_when_requested(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.post("1" * 26, LOUNGE, ALICE, "archived lighthouse")
    turn.rest.file("f" * 26, LOUNGE, "archived-lighthouse.txt", b"text")
    turn.rest.channels[LOUNGE]["delete_at"] = 1

    posts = await turn.call("search_messages", query="lighthouse")
    files = await turn.call("search_mattermost_files", query="lighthouse")
    with_posts = await turn.call("search_messages", query="lighthouse", include_archived_channels=True)
    with_files = await turn.call("search_mattermost_files", query="lighthouse", include_archived_channels=True)

    assert posts["messages"] == [] and files["files"] == []
    assert [message["text"] for message in with_posts["messages"]] == ["archived lighthouse"]
    assert [item["name"] for item in with_files["files"]] == ["archived-lighthouse.txt"]


# ── find_by_name ─────────────────────────────────────────────────────────


async def test_people_channels_and_emoji_are_found_by_any_of_their_names_without_an_email(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.users[ALICE].update(first_name="Alice", last_name="Quayle", nickname="ally", email="alice@example.invalid")
    turn.rest.emoji.append("quayside")

    found = await turn.call("find_by_name", query="qua")

    by_kind = {(match["kind"], match["id"]): match for match in found["matches"]}
    assert by_kind[("person", ALICE)]["matched"] == "real_name"
    assert by_kind[("emoji", "quayside")]["match_type"] == "prefix"
    assert "email" not in str(found)
    assert found["coverage"]["kinds_searched"] == ["person", "chat", "emoji"]


async def test_a_private_channel_is_found_only_where_the_bot_is_a_member_and_coverage_says_so(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.channels["s" * 26] = {"id": "s" * 26, "type": "P", "team_id": TEAM, "name": "quiet-secret", "display_name": "Quiet Secret"}

    found = await turn.call("find_by_name", query="quiet", kind="chat")

    assert [(match["id"], match["is_private"]) for match in found["matches"]] == [(QUIET, True)]
    assert "never found" in found["coverage"]["chat_scope_note"]


async def test_a_kind_the_server_refuses_is_named_and_a_lookup_it_refuses_whole_is_no_empty_answer(turn):
    turn.came_from(LOUNGE, chat_type="O")
    turn.rest.routes[("GET", "/emoji")] = (501, {"id": "api.emoji.disabled.app_error", "status_code": 501})

    partial = await turn.call("find_by_name", query="bob")
    refused = await turn.call("find_by_name", query="bob", kind="emoji")
    unknown = await turn.call("find_by_name", query="bob", kind="usergroup")

    assert [match["id"] for match in partial["matches"]] == [BOB]
    assert partial["coverage"]["kinds_unavailable"] == ["emoji"] and partial["coverage"]["status"] == "partial"
    assert refused["ok"] is False and "not an empty result" in refused["detail"]
    assert unknown["error"] == "unknown_kind" and "person, chat, emoji" in unknown["detail"]
    assert CAROL not in str(partial["matches"])
