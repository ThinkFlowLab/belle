"""Exercise the resource browser with Textual's headless driver."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.color import Color, ColorSystem
from textual.widgets import DataTable, Static, Tab, Tabs

from belle_agent.control.client import get_snapshot
from belle_agent.control.service import ControlSettings, SnapshotCollector, StatusService
from belle_agent.tui import app as module
from belle_agent.tui.app import (
    BelleApp,
    ConfirmAction,
    Details,
    PeekScreen,
    format_peek_duration,
    format_peek_outcome,
)


def test_format_peek_outcome_line():
    assert format_peek_duration(5) == "5s"
    assert format_peek_duration(65) == "1m 05s"
    assert format_peek_outcome({
        "state": "succeeded",
        "duration_seconds": 65,
        "ended_at": "2026-10-20T14:32:01+00:00",
        "extra": "posted-final",
    }) == "Worked for 1m 05s - 14:32 - posted-final"
    assert format_peek_outcome({
        "state": "failed", "duration_seconds": None, "ended_at": None, "extra": None,
    }) == "Worked for unknown time"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Stop the clock of the TUI when the test starts. A test moves it with ``clock[0]``."""
    now = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(module, "datetime", Clock)
    return now


async def settle(pilot, done):
    """Let the app run until ``done()`` holds, for five seconds at most."""
    for _ in range(100):
        if done():
            return
        await pilot.pause(0.05)
    assert done()


def snapshot(count=2):
    now = module.datetime.now(UTC).isoformat()
    component = {"id": "fixture", "state": "unsupported", "reason": "not measured", "observed_at": now,
                 "metrics": {"active_turns": {"value": None, "reason": "unsupported"}}}
    harness = {**deepcopy(component), "mcp": {"state": "not_applicable", "reason": None, "observed_at": None}}
    measured = deepcopy(component)
    measured.update(state="ready", reason=None)
    measured["metrics"] = {"active_turns": {"value": 0, "reason": None}}
    measured["rows"] = [
        {"id": f"local-execution-{index}", "state": "running" if index % 2 == 0 else "queued",
         "type": "main", "agent": None, "parent": None,
         "platform": "slack", "workspace": "workspace-id", "conversation": "channel-thread-identity", "harness": "scripted",
         "started_at": now if index % 2 == 0 else None, "model": None,
         "tokens_in": 0 if index == 0 else None, "tokens_out": None}
        for index in range(count)]
    return {"instance_id": "d7761202-6055-4b09-9dad-934e26679592", "generated_at": now, "platforms": [component],
            "harnesses": [harness], "work": measured, "scheduling": component,
            "task_models": [
                {"role": "decision", "id": "judge", "api": "openrouter", "model_name": "decision-model", "selected": True},
                {"role": "summarizer", "id": "brief", "api": "scaledown", "model_name": "summarize", "selected": True},
                {"role": "extractor", "id": "extract", "api": "scaledown", "model_name": "extract", "selected": True},
                {"role": "classifier", "id": "classify", "api": "scaledown", "model_name": "classify-1", "selected": True},
            ],
            "harness_models": [{"owner": "opencode", "model_name": "openai/gpt-5"}]}


def content(app, selector):
    return str(app.query_one(selector, Static).content)


def health_color(app, symbol):
    header = app.query_one("#connection", Static)
    column = header.region.x + 1 + content(app, "#connection").index(symbol)
    strip = app.screen._compositor.render_strips()[header.region.y]
    return next(iter(strip.crop(column, column + 1))).style.color


def install(monkeypatch, data):
    async def fetch(_):
        return data
    monkeypatch.setattr(module, "get_snapshot", fetch)


def column_cells(table, label):
    index = next(index for index, column in enumerate(table.columns.values())
                 if column.label.plain.startswith(label[:5]))
    return [table.get_row_at(row)[index].plain for row in range(table.row_count)]


@pytest.mark.parametrize("width", [60, 80, 100, 140])
@pytest.mark.parametrize("theme", ["dark", "light"])
async def test_resource_navigation_and_details(monkeypatch, width, theme):
    data = snapshot()
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"), theme=theme)
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert app.active_view == "work"
        assert app.query_one(Tabs).active == "work"
        assert [tab.id for tab in app.query(Tab)] == ["work", "scheduling", "platforms", "harnesses", "models"]
        assert all(tab.label.plain.startswith(" ") for tab in list(app.query(Tab))[1:])
        assert "Activity" in str(app.query_one("#work", Tab).label)
        assert app.focused is table
        assert all(tab.region.right <= width for tab in app.query(Tab))
        tab_row = app.screen._compositor.render_strips()[app.query_one("#views").region.y]
        rendered_tabs = "".join(part.text for part in tab_row)
        gap = "  " if width >= 64 else " "
        for label, number in (("Activity", 2), ("Jobs", 3), ("Platforms", 4), ("Harnesses", 5)):
            assert f"{label}{gap}[{number}]" in rendered_tabs
        assert table.row_count == 2
        assert table.region.height >= 16
        assert table.region.right <= width
        assert table.max_scroll_x == 0
        assert "● CONNECTED" in content(app, "#connection")
        updated = datetime.fromisoformat(data["generated_at"]).astimezone().strftime("%H:%M:%S")
        assert updated in content(app, "#connection")
        assert content(app, "#connection").lstrip().startswith("Belle")
        assert "dev" not in content(app, "#connection")
        assert "age" not in content(app, "#connection")
        assert cell_len(content(app, "#connection")) == width - 2
        strips = app.screen._compositor.render_strips()
        chrome = [app.query_one(name) for name in ("#connection", "#views", "#keys")]
        surface = Color.parse(app.get_css_variables()["surface"])
        assert all(next(iter(strips[widget.region.y].crop(0, 1))).style.bgcolor == surface for widget in chrome)
        footer = app.query_one("#keys")
        assert next(iter(strips[footer.region.y].crop(1, 2))).style.color == Color.parse(
            app.get_css_variables()["accent"])
        assert next(iter(strips[footer.region.y].crop(5, 6))).style.color != Color.parse(
            app.get_css_variables()["accent"])
        assert health_color(app, "●") == Color.parse(app.state_color("ready"))
        selected_tab = app.query_one("#work", Tab)
        tab_style = next(iter(strips[selected_tab.region.y].crop(selected_tab.region.x + 4, selected_tab.region.x + 5))).style
        header_style = next(iter(strips[table.region.y].crop(2, 3))).style
        assert tab_style.bgcolor == surface
        assert header_style.bgcolor == Color.parse(app.get_css_variables()["accent"])
        selected = next(iter(strips[table.region.y + 1].crop(2, 3))).style.bgcolor
        app.set_focus(None)
        await pilot.pause()
        strips = app.screen._compositor.render_strips()
        assert next(iter(strips[table.region.y + 1].crop(2, 3))).style.bgcolor == selected
        table.focus()
        assert app.query_one("#connection").region.height == 1
        assert app.theme == f"belle-{theme}"
        assert app.get_theme(app.theme).accent == ("#8a5a10" if theme == "light" else "#d4a04a")
        assert "slack" in str(table.get_row_at(0))
        await pilot.press("down", "enter")
        assert isinstance(app.screen, Details)
        detail = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert "local-execution-1" in detail
        assert "channel-thread-identity" in detail
        assert "scripted" in detail and "MODEL" in detail and "—" in detail
        await pilot.press("escape", "3")
        assert app.active_view == "platforms"
        assert "unsupported" in str(table.get_row_at(0))
        await pilot.press("4", "5", "1")
        assert app.active_view == "work"
        await pilot.press("q")
        assert not app.is_running


async def test_activity_peek_opens_and_closes(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)

    async def open_peek(socket, work_id, instance_id):
        assert work_id == "local-execution-0"
        assert instance_id == data["instance_id"]
        return {"peek_id": "peek-test", "work_id": work_id, "live": True,
                "frames": [{"seq": 1, "at": data["generated_at"], "work_id": work_id,
                            "source_work_id": work_id, "kind": "text", "payload": {"text": "hello"}}]}

    async def poll_peek(socket, peek_id, after_seq):
        return {"peek_id": peek_id, "work_id": "local-execution-0", "live": True, "frames": []}

    closed = []

    async def close_peek(socket, peek_id):
        closed.append(peek_id)

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        assert "hello" in app.screen._open_buf
        await pilot.press("escape")
        assert not isinstance(app.screen, PeekScreen)
        assert closed == ["peek-test"]


async def test_peek_esc_during_open_closes_returned_id(monkeypatch):
    """Esc while peek_open is in flight must still close the subscriber the server allocated."""
    import asyncio

    data = snapshot(count=1)
    install(monkeypatch, data)
    open_started = asyncio.Event()
    gate = asyncio.Event()
    closed: list[str] = []

    async def open_peek(socket, work_id, instance_id):
        open_started.set()
        await gate.wait()
        return {
            "peek_id": "peek-late", "work_id": work_id, "live": True, "lost": 0,
            "frames": [],
        }

    async def poll_peek(socket, peek_id, after_seq):
        raise AssertionError("poll should not run after Esc during open")

    async def close_peek(socket, peek_id):
        closed.append(peek_id)

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await open_started.wait()
        await pilot.press("escape")
        assert not isinstance(app.screen, PeekScreen)
        assert closed == []
        gate.set()
        await app.workers.wait_for_complete()
        assert closed == ["peek-late"]


async def test_peek_tool_row_expands_nested_body(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-tools", "work_id": work_id, "live": True,
            "frames": [
                {
                    "seq": 1, "at": data["generated_at"], "work_id": work_id,
                    "source_work_id": work_id, "kind": "tool_call",
                    "payload": {
                        "call_id": "c1", "name": "shell", "detail": "list",
                        "args_text": '{\n  "command": "ls"\n}',
                    },
                },
                {
                    "seq": 2, "at": data["generated_at"], "work_id": work_id,
                    "source_work_id": work_id, "kind": "text",
                    "payload": {"text": "after tool\n"},
                },
            ],
        }

    async def poll_peek(socket, peek_id, after_seq):
        return {"peek_id": peek_id, "work_id": "local-execution-0", "live": True, "frames": []}

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        rows = list(app.screen.query(module.PeekToolRow))
        assert len(rows) == 1
        row = rows[0]
        assert row.expanded is False
        assert row.body is not None and "ls" in row.body
        meta = str(row.query_one("#tool-meta", Static).content)
        assert "→ shell · list" in meta and "+" in meta
        # Collapsed: nested body scroll is not shown.
        assert "-expanded" not in row.classes
        await pilot.press("+")
        await pilot.pause()
        assert row.expanded is True
        assert "-expanded" in row.classes
        marker = row.query_one("#tool-marker", Static).content
        assert (marker.plain if hasattr(marker, "plain") else str(marker)) == "  └ "
        body = str(row.query_one("#tool-body", Static).content)
        assert "ls" in body
        assert "└" not in body
        meta = str(row.query_one("#tool-meta", Static).content)
        assert not meta.endswith("\n")
        assert "after tool" in app.screen._open_buf
        assert app.screen._open_kind == "text"
        await pilot.press("-")
        await pilot.pause()
        assert row.expanded is False
        await pilot.press("=")
        await pilot.pause()
        assert row.expanded is True
        await pilot.press("=")
        await pilot.pause()
        assert row.expanded is True
        await pilot.press("escape")


async def test_peek_closes_server_when_turn_ends_on_open(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)
    closed = []

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-done", "work_id": work_id, "live": False,
            "frames": [{
                "seq": 1, "at": data["generated_at"], "work_id": work_id,
                "source_work_id": work_id, "kind": "outcome",
                "payload": {
                    "state": "failed", "duration_seconds": 3,
                    "ended_at": data["generated_at"], "extra": "boom",
                },
            }],
        }

    async def poll_peek(socket, peek_id, after_seq):
        raise AssertionError("poll should not run after ended open")

    async def close_peek(socket, peek_id):
        closed.append(peek_id)

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        assert app.screen._ended is True
        assert app.screen.peek_id is None
        await pilot.pause()
        assert closed == ["peek-done"]
        await pilot.press("escape")
        assert closed == ["peek-done"]


async def test_peek_poll_timeout_keeps_stream_and_recovers(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)
    polls = {"n": 0}

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-retry", "work_id": work_id, "live": True, "lost": 0,
            "frames": [{
                "seq": 1, "at": data["generated_at"], "work_id": work_id,
                "source_work_id": work_id, "kind": "text", "payload": {"text": "kept\n"},
            }],
        }

    async def poll_peek(socket, peek_id, after_seq):
        polls["n"] += 1
        if polls["n"] == 1:
            raise TimeoutError
        return {
            "peek_id": peek_id, "work_id": "local-execution-0", "live": True, "lost": 0,
            "frames": [{
                "seq": 2, "at": data["generated_at"], "work_id": "local-execution-0",
                "source_work_id": "local-execution-0", "kind": "text", "payload": {"text": "again\n"},
            }],
        }

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        assert "kept" in app.screen._open_buf
        assert app.screen._ended is False
        await settle(pilot, lambda: polls["n"] >= 1 and not app.screen._polling)
        assert "kept" in app.screen._open_buf
        await settle(pilot, lambda: "again" in app.screen._open_buf)
        assert "kept" in app.screen._open_buf
        await pilot.press("escape")


async def test_peek_keyboard_scroll_pauses_follow(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-follow", "work_id": work_id, "live": True, "lost": 0,
            "frames": [{
                "seq": 1, "at": data["generated_at"], "work_id": work_id,
                "source_work_id": work_id, "kind": "text",
                "payload": {"text": ("line\n" * 80)},
            }],
        }

    async def poll_peek(socket, peek_id, after_seq):
        return {"peek_id": peek_id, "work_id": "local-execution-0", "live": True, "frames": [], "lost": 0}

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        assert app.screen._follow is True
        await pilot.press("pageup")
        assert app.screen._follow is False
        await pilot.press("escape")


async def test_peek_keeps_all_server_frames_without_client_cap(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)
    at = data["generated_at"]
    work_id = "local-execution-0"

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-keep", "work_id": work_id, "live": True, "lost": 0,
            "frames": [
                {
                    "seq": 1, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "text", "payload": {"text": "first\n"},
                },
                {
                    "seq": 2, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "tool_call",
                    "payload": {"call_id": "c1", "name": "shell", "detail": None, "args_text": None},
                },
                {
                    "seq": 3, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "text", "payload": {"text": "third\n"},
                },
            ],
        }

    async def poll_peek(socket, peek_id, after_seq):
        return {"peek_id": peek_id, "work_id": work_id, "live": True, "frames": [], "lost": 0}

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        kinds = [type(widget).__name__ for widget in app.screen._widgets]
        assert kinds == ['Static', 'PeekToolRow', 'Static']
        assert app.screen._open_buf == "third\n"
        await pilot.press("escape")


async def test_peek_coalesces_text_across_polls(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)
    at = data["generated_at"]
    work_id = "local-execution-0"
    polls = {"n": 0}

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-coal", "work_id": work_id, "live": True, "lost": 0,
            "frames": [{
                "seq": 1, "at": at, "work_id": work_id, "source_work_id": work_id,
                "kind": "text", "payload": {"text": "hel"},
            }],
        }

    async def poll_peek(socket, peek_id, after_seq):
        polls["n"] += 1
        if polls["n"] != 1:
            return {"peek_id": peek_id, "work_id": work_id, "live": True, "frames": [], "lost": 0}
        return {
            "peek_id": peek_id, "work_id": work_id, "live": True, "lost": 0,
            "frames": [{
                "seq": 2, "at": at, "work_id": work_id, "source_work_id": work_id,
                "kind": "text", "payload": {"text": "lo"},
            }],
        }

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        assert app.screen._open_buf == "hel"
        assert len([w for w in app.screen._widgets if type(w).__name__ == "Static"]) == 1
        await settle(pilot, lambda: app.screen._open_buf == "hello")
        assert len([w for w in app.screen._widgets if type(w).__name__ == "Static"]) == 1
        await pilot.press("escape")


async def test_peek_stream_text_excludes_tool_rows(monkeypatch):
    data = snapshot(count=1)
    install(monkeypatch, data)
    at = data["generated_at"]
    work_id = "local-execution-0"

    async def open_peek(socket, work_id, instance_id):
        return {
            "peek_id": "peek-parts", "work_id": work_id, "live": True,
            "frames": [
                {
                    "seq": 1, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "tool_call",
                    "payload": {"call_id": "c1", "name": "shell", "detail": "x", "args_text": "{}"},
                },
                {
                    "seq": 2, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "text", "payload": {"text": "hello\n"},
                },
                {
                    "seq": 3, "at": at, "work_id": work_id, "source_work_id": work_id,
                    "kind": "tool_result",
                    "payload": {"call_id": "c1", "name": "shell", "ok": True, "summary": None},
                },
            ],
        }

    async def poll_peek(socket, peek_id, after_seq):
        return {"peek_id": peek_id, "work_id": work_id, "live": True, "frames": []}

    async def close_peek(socket, peek_id):
        return None

    monkeypatch.setattr(module, "peek_open", open_peek)
    monkeypatch.setattr(module, "peek_poll", poll_peek)
    monkeypatch.setattr(module, "peek_close", close_peek)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert isinstance(app.screen, PeekScreen)
        await pilot.pause()
        # Tool result flushed the text Static; open buf is empty until more text.
        assert sum(1 for w in app.screen._widgets if type(w).__name__ == "Static") == 1
        assert len(list(app.screen.query(module.PeekToolRow))) == 2
        await pilot.press("escape")


@pytest.mark.parametrize(("system", "accent"), [
    (ColorSystem.STANDARD, "yellow"),
    (ColorSystem.EIGHT_BIT, "#d4a04a"),
    (ColorSystem.TRUECOLOR, "#d4a04a"),
])
async def test_terminal_color_fallback(monkeypatch, system, accent):
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    base = app.get_theme("textual-dark")
    monkeypatch.setattr(app.console, "_color_system", system)
    async with app.run_test():
        theme = app.get_theme(app.theme)
        assert theme.accent == accent
        if system == ColorSystem.STANDARD:
            assert (theme.background, theme.foreground, theme.surface, theme.panel) == (
                base.background, base.foreground, base.surface, base.panel)
        else:
            assert (theme.background, theme.foreground, theme.surface, theme.panel) == (
                "#0f1419", "#e8eef4", "#1a222c", "#243040")
        assert app.state_color("ready") == ("green" if system == ColorSystem.STANDARD else "#73c991")
        assert app.state_color("completed") == ("white" if system == ColorSystem.STANDARD else "#8b9298")


@pytest.mark.parametrize("width,maximum_rings", [(60, 0), (80, 3)])
async def test_brand_burst_rings_outward_without_moving_status(monkeypatch, width, maximum_rings):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test(size=(width, 24)):
        delays = []
        monkeypatch.setattr(app, "set_timer", lambda delay, callback: delays.append(delay))
        idle = app.query_one("#connection", Static).content.plain
        assert idle.lstrip().startswith("Belle")
        assert "(" not in idle and ")" not in idle
        columns = {label: idle.index(label) for label in ("Belle", "● CONNECTED", "running", "queued")}
        assert columns["Belle"] == (4 if maximum_rings else 0)
        assert list(columns.values()) == sorted(columns.values())
        app._start_brand_burst()
        for frame in range(app.BRAND_BURST_FRAMES):
            header = app.query_one("#connection", Static).content
            phase = frame % 9
            depth = min((0, 0, 0, 1, 2, 3, 2, 1, 0)[phase], maximum_rings)
            assert "● CONNECTED" in header.plain
            assert {label: header.plain.index(label) for label in columns} == columns
            assert header.plain.count("(") == depth
            assert header.plain.count(")") == header.plain.count("(")
            assert cell_len(header.plain) == width - 2
            if phase == 0:
                assert any(span.start <= header.plain.index("l") < span.end and "#fff2be" in str(span.style).lower()
                           for span in header.spans)
            if phase == 5 and maximum_rings == 3:
                assert header.plain.startswith("((( Belle )))")
                assert any(span.start == 2 and "bold" in str(span.style) for span in header.spans)
                assert any(span.start == 0 and "#80602c" in str(span.style).lower()
                           for span in header.spans)
                outer_right = header.plain.rfind(")")
                assert header.plain[outer_right + 1] == " "
                assert any(span.start <= outer_right < span.end and "#80602c" in str(span.style).lower()
                           for span in header.spans)
            if maximum_rings == 3 and phase == 6:
                assert header.plain.startswith("((  Belle  ))")
            if maximum_rings == 3 and phase == 7:
                assert header.plain.startswith("(   Belle   )")
            app._advance_brand_burst()
        assert app._brand_burst_frame == -1
        assert app.query_one("#connection", Static).content.plain == idle
        assert 0.09 <= app.BRAND_BURST_FRAME_SECONDS <= 0.10
        assert delays == [app.BRAND_BURST_FRAME_SECONDS] * app.BRAND_BURST_FRAMES


@pytest.mark.parametrize("system", [ColorSystem.STANDARD, None])
async def test_brand_burst_color_fallback_and_motion_off(monkeypatch, system):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", system)
    async with app.run_test(size=(80, 24)):
        monkeypatch.setattr(app, "set_timer", lambda delay, callback: None)
        idle = app.query_one("#connection", Static).content.plain
        app._start_brand_burst()
        if system is None:
            assert app._brand_burst_frame == -1
            assert app.query_one("#connection", Static).content.plain == idle
        else:
            first = app.query_one("#connection", Static).content
            assert any("reverse" in str(span.style) for span in first.spans)
            app._brand_burst_frame = 5
            rings = app.status_header("—", "—", "connected", "update unknown")
            assert "queued — | update unknown" in rings.plain
            outer_right = rings.plain.rfind(")")
            assert rings.plain[outer_right + 1] == " "
            assert any(span.start <= outer_right < span.end and "dim" in str(span.style)
                       for span in rings.spans)
            app._brand_burst_frame = 0
            assert all("#fff2be" not in str(span.style).lower() for span in first.spans)
            for _ in range(5):
                app._advance_brand_burst()
            header = app.query_one("#connection", Static).content
            assert "((( Belle )))" in header.plain
        for variable in ("NO_COLOR", "TEXTUAL_ANIMATIONS"):
            monkeypatch.setenv(variable, "1" if variable == "NO_COLOR" else "none")
            app._brand_burst_frame = -1
            app.render_header()
            app._start_brand_burst()
            assert app._brand_burst_frame == -1
            assert app.query_one("#connection", Static).content.plain == idle
            monkeypatch.delenv(variable)


async def test_clicking_belle_starts_burst_without_resetting_interval(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test(size=(80, 24)) as pilot:
        intervals = []
        monkeypatch.setattr(app, "set_interval", lambda *args: intervals.append(args))
        monkeypatch.setattr(app, "set_timer", lambda *_: None)
        header = app.query_one("#connection", Static)
        brand_x = header.content.plain.index("Belle") + 1  # Account for header padding.
        await pilot.click(header, offset=(brand_x - 1, 0))
        assert app._brand_burst_frame == -1
        await pilot.click(header, offset=(brand_x + 1, 0))
        assert app._brand_burst_frame == 0
        assert not intervals
        assert app.BRAND_BURST_INTERVAL_SECONDS == 10


async def test_inactive_rows_are_dimmed(monkeypatch):
    data = snapshot()
    parent = data["work"]["rows"][0]
    parent["id"] = "turn-a"
    data["work"]["rows"].append({**parent, "id": "child-a", "state": "completed", "type": "subagent",
                                 "agent": "research", "parent": "turn-a"})
    data["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                          "observed_at": data["generated_at"], "metrics": {}, "rows": [
        {"job_id": f"job-{state}", "name": state, "state": state,
         "when": "every 5 minutes", "next_due": None}
        for state in ("active", "paused", "ended")]}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert column_cells(table, "STATE") == ["running", "queued", "completed"]
        assert all("dim" not in cell.style for cell in table.get_row_at(0))
        assert all("dim" in cell.style for cell in table.get_row_at(1))
        assert all("#8b9298" in cell.style for cell in table.get_row_at(2))
        def selected_brightness(row):
            strips = app.screen._compositor.render_strips()
            color = next(iter(strips[table.region.y + row + 1].crop(table.region.x + 14, table.region.x + 15))).style.color
            return sum(color.triplet)
        running = selected_brightness(0)
        await pilot.press("down")
        assert selected_brightness(1) < running
        await pilot.press("2")
        assert all("dim" not in cell.style for cell in table.get_row_at(0))
        for row in (1, 2):
            assert all("dim" in cell.style for cell in table.get_row_at(row))


async def test_subagents_have_list_and_tree_rows_until_parent_ends(monkeypatch):
    data = snapshot(2)
    parent = data["work"]["rows"][0]
    parent["id"] = "turn-a"
    second = data["work"]["rows"][1]
    second.update(id="turn-b", state="running", started_at=parent["started_at"])
    children = [
        {**parent, "id": "child-a", "state": "completed", "type": "subagent",
         "agent": "research", "parent": "turn-a"},
        {**parent, "id": "child-b", "state": "running", "type": "subagent",
         "agent": "code", "parent": "turn-b"},
    ]
    data["work"]["rows"] = [parent, *children, second]
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 4
        assert "PARENT" in [column.label.plain for column in table.columns.values()]
        completed = column_cells(table, "LOCAL ID").index("child-a")
        assert column_cells(table, "PARENT")[completed] == "turn-a"
        assert all("dim" in cell.style for cell in table.get_row_at(completed))
        await pilot.press("t")
        assert column_cells(table, "LOCAL ID") == ["turn-a", "child-a", "turn-b", "child-b"]
        assert "PARENT" not in [column.label.plain for column in table.columns.values()]
        assert column_cells(table, "AGENT")[1].startswith("└─ ")
        table.move_cursor(row=1)
        await pilot.press("enter")
        assert "research" in " ".join(str(widget.content) for widget in app.screen.query(Static))
        await pilot.press("escape")
        await pilot.press("t")
        assert "PARENT" in [column.label.plain for column in table.columns.values()]
        data["work"]["rows"] = [second, children[1]]
        app.render_snapshot()
        assert column_cells(table, "LOCAL ID") == ["turn-b", "child-b"]


async def test_state_sort_is_default_in_every_tab(monkeypatch):
    data = snapshot()
    for state in ("active", "ready", "running"):
        row = deepcopy(data["platforms"][0])
        row.update(id=state, state=state, reason=None)
        data["platforms"].append(row)
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        for tab in ("1", "2", "3", "4"):
            await pilot.press(tab)
            assert "↑" in next(iter(table.columns.values())).label.plain
            if tab == "3":
                assert column_cells(table, "STATE") == ["running", "ready", "active", "unsupported"]
        await pilot.press("5")
        assert [column.label.plain for column in table.columns.values()][1] == "ROLE ↑"


async def test_component_tabs_name_available_fields(monkeypatch):
    data = snapshot()
    for group in ("platforms", "harnesses"):
        data[group][0]["reason"] = "remote_health_not_measured"
        data[group][0]["metrics"] = {}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        await pilot.press("3")
        labels = [column.label.plain for column in table.columns.values()]
        assert labels[1:] == ["PLATFORM", "DETAIL"]
        assert table.get_row_at(0)[2].plain == "remote health not measured"
        await pilot.press("4")
        labels = [column.label.plain for column in table.columns.values()]
        assert labels[1:] == ["HARNESS", "MCP", "HEALTH"]
        assert table.get_row_at(0)[2].plain == "n/a"
        assert table.get_row_at(0)[3].plain == "remote health not measured"
        await pilot.press("enter")
        detail = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert "remote_health_not_measured" in detail
        assert "MCP" in detail and "n/a" in detail


async def test_harness_tab_shows_degraded_health_reason(monkeypatch):
    data = snapshot()
    data["harnesses"][0].update(
        state="degraded", reason="event stream reconnecting", observed_at=data["generated_at"],
    )
    data["harnesses"][0]["metrics"] = {}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test() as pilot:
        await pilot.press("4")
        table = app.query_one(DataTable)
        assert column_cells(table, "HEALTH") == ["event stream reconnecting"]


@pytest.mark.parametrize("mcp_state,label,reason", [
    ("ready", "ready", None),
    ("missing", "missing", "mcp_mount_missing"),
    ("failed", "failed", "mcp_mount_failed"),
    ("unknown", "unknown", "mcp_status_unavailable"),
    ("not_applicable", "n/a", None),
])
async def test_harness_tab_shows_mcp_column(monkeypatch, mcp_state, label, reason):
    data = snapshot()
    data["harnesses"][0]["mcp"] = {
        "state": mcp_state, "reason": reason, "observed_at": data["generated_at"],
    }
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.press("4")
        table = app.query_one(DataTable)
        assert [column.label.plain for column in table.columns.values()][2] == "MCP"
        assert column_cells(table, "MCP") == [label]
        await pilot.press("enter")
        detail = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert label in detail
        if reason:
            assert reason in detail


async def test_harness_tab_sorts_by_mcp(monkeypatch):
    data = snapshot()
    data["harnesses"] = [
        {**data["harnesses"][0], "id": "a", "mcp": {
            "state": "failed", "reason": "mcp_mount_failed", "observed_at": data["generated_at"],
        }},
        {**data["harnesses"][0], "id": "b", "mcp": {
            "state": "ready", "reason": None, "observed_at": data["generated_at"],
        }},
        {**data["harnesses"][0], "id": "c", "mcp": {
            "state": "unknown", "reason": "x", "observed_at": data["generated_at"],
        }},
    ]
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("4")
        app.cycle_sort("mcp")
        assert column_cells(app.query_one(DataTable), "MCP") == ["ready", "unknown", "failed"]


@pytest.mark.parametrize("width", [60, 80, 100])
async def test_belle_and_harness_models_show_owner_and_safe_details(monkeypatch, width):
    data = snapshot()
    if width == 60:
        data["harness_models"][0]["owner"] = "jiuwenswarm"
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.press("5")
        table = app.query_one(DataTable)
        assert table.row_count == 5
        assert table.max_scroll_x == 0
        labels = [column.label.plain for column in table.columns.values()]
        assert labels[:4] == ["OWNER", "ROLE ↑", "ENTRY", "MODEL"]
        assert ("API" in labels) == (width >= 90)
        assert labels[-1] == "SELECTED"
        assert column_cells(table, "ROLE") == ["classifier", "decision", "extractor", "harness", "summarizer"]
        owner = "jiuwenswarm" if width == 60 else "opencode"
        assert column_cells(table, "OWNER") == ["Belle", "Belle", "Belle", owner, "Belle"]
        table.move_cursor(row=1)
        assert table.get_row_at(1)[-1].plain == "yes"
        await pilot.press("enter")
        details = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert "openrouter" in details
        assert "decision-model" in details
        assert "api_key" not in details
        await pilot.press("escape")
        await pilot.press("down", "down", "enter")
        details = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert owner in details
        assert "openai/gpt-5" in details


async def test_model_selection_stays_on_same_harness_model_after_refresh(monkeypatch):
    data = snapshot()
    data["harness_models"].append({"owner": "opencode", "model_name": "openai/gpt-4"})
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("5")
        table = app.query_one(DataTable)
        table.move_cursor(row=4)
        assert table.get_row_at(table.cursor_row)[3].plain == "openai/gpt-4"
        data["harness_models"].reverse()
        app.render_snapshot()
        assert table.get_row_at(table.cursor_row)[3].plain == "openai/gpt-4"


async def test_models_explain_unavailable_harness_catalog(monkeypatch):
    data = snapshot()
    data["task_models"] = []
    data["harness_models"] = None
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("5")
        assert "harness model list unavailable" in content(app, "#keys")


async def test_tab_number_style_matches_mouse_and_keyboard_selection(monkeypatch):
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        tab = app.query_one("#scheduling", Tab)

        def number_style():
            strips = app.screen._compositor.render_strips()
            return next(iter(strips[tab.region.y].crop(tab.region.x + 1, tab.region.x + 2))).style

        keyboard_style = number_style()
        await pilot.click("#platforms", offset=(1, 0))
        await pilot.click(tab, offset=(1, 0))
        await pilot.pause()
        assert number_style() == keyboard_style


async def test_header_hover_highlights_only_one_column(monkeypatch):
    data = snapshot()
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        await pilot.hover(table, offset=(3, 0))
        strips = app.screen._compositor.render_strips()
        header = strips[table.region.y]
        first = next(iter(header.crop(table.region.x + 3, table.region.x + 4))).style.bgcolor
        second = next(iter(header.crop(table.region.x + 12, table.region.x + 13))).style.bgcolor
        assert first != second
        assert second == Color.parse(app.get_css_variables()["accent"])
        data["work"]["rows"][0]["tokens_in"] = 1
        app.render_snapshot()
        await pilot.pause()
        header = app.screen._compositor.render_strips()[table.region.y]
        first = next(iter(header.crop(table.region.x + 3, table.region.x + 4))).style.bgcolor
        second = next(iter(header.crop(table.region.x + 12, table.region.x + 13))).style.bgcolor
        assert first != second
        assert second == Color.parse(app.get_css_variables()["accent"])
        await pilot.hover(table, offset=(3, 1))
        row = app.screen._compositor.render_strips()[table.region.y + 1]
        first = next(iter(row.crop(table.region.x + 3, table.region.x + 4))).style.bgcolor
        second = next(iter(row.crop(table.region.x + 12, table.region.x + 13))).style.bgcolor
        assert first == second


async def test_large_table_keeps_selection_and_scroll_on_poll(monkeypatch):
    data = snapshot(1000)
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 1000
        await pilot.press("end")
        await pilot.pause()
        selected = table.cursor_row
        app.render_snapshot()
        assert table.cursor_row == selected
        assert table.max_scroll_y > 0
        await pilot.resize_terminal(80, 24)
        assert table.cursor_row == selected
        assert table.max_scroll_x == 0


async def test_header_click_cycles_sort_and_keeps_selected_row(monkeypatch):
    data = snapshot(4)
    for index, row in enumerate(data["work"]["rows"]):
        row["id"] = f"id-{index}"
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        table.move_cursor(row=0)
        ids = lambda: column_cells(table, "LOCAL ID")
        assert ids() == ["id-0", "id-2", "id-1", "id-3"]
        await pilot.click(table, offset=(60, 0))
        assert ids() == [f"id-{index}" for index in range(4)]
        assert any("↑" in column.label.plain for column in table.columns.values())
        await pilot.click(table, offset=(60, 0))
        assert ids() == [f"id-{index}" for index in reversed(range(4))]
        assert ids()[table.cursor_row] == "id-0"
        await pilot.press("enter")
        assert "id-0" in " ".join(str(widget.content) for widget in app.screen.query(Static))
        await pilot.press("escape")
        await pilot.click(table, offset=(60, 0))
        assert ids() == [f"id-{index}" for index in range(4)]
        assert ids()[table.cursor_row] == "id-0"


async def test_keyboard_sort_uses_raw_numeric_values_and_survives_refresh(monkeypatch):
    data = snapshot(4)
    for index, (row, tokens) in enumerate(zip(data["work"]["rows"], (20, None, 3, 100), strict=True)):
        row["id"] = f"id-{index}"
        row["tokens_in"] = tokens
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        await pilot.press(*("c",) * 10, "s")
        assert column_cells(table, "IN") == ["3", "20", "100", "—"]
        data["work"]["rows"][0]["tokens_in"] = 1
        app.render_snapshot()
        assert column_cells(table, "IN") == ["1", "3", "100", "—"]
        await pilot.press("s")
        assert column_cells(table, "IN") == ["100", "3", "1", "—"]
        await pilot.press("s")
        assert column_cells(table, "LOCAL ID") == [
            f"id-{index}" for index in range(4)]


async def test_time_plus_tracks_running_wall_time(monkeypatch, clock):
    data = snapshot()
    data["work"]["rows"][0]["started_at"] = (clock[0] - timedelta(seconds=65)).isoformat()
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert "TIME+" in str(table.columns.values())
        assert column_cells(table, "TIME+") == ["01:05", "—"]
        clock[0] += timedelta(seconds=1)
        app.render_snapshot()
        assert column_cells(table, "TIME+")[0] == "01:06"
        app.cycle_sort("elapsed_seconds")
        assert column_cells(table, "TIME+")[1] == "—"


async def test_sort_is_independent_per_view(monkeypatch):
    data = snapshot(3)
    for index, row in enumerate(data["work"]["rows"]):
        row["id"] = f"id-{index}"
    data["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                          "observed_at": data["generated_at"], "metrics": {}, "rows": [
        {"job_id": f"job-{index}", "name": name, "state": "active", "when": "at 09:00", "next_due": None}
        for index, name in enumerate(("Zeta", "Alpha", "Middle"))]}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("2", "c", "s")
        table = app.query_one(DataTable)
        assert [table.get_row_at(index)[1].plain for index in range(3)] == ["Alpha", "Middle", "Zeta"]
        await pilot.press("1")
        assert column_cells(table, "LOCAL ID") == ["id-0", "id-2", "id-1"]
        await pilot.press("2")
        assert [table.get_row_at(index)[1].plain for index in range(3)] == ["Alpha", "Middle", "Zeta"]


async def test_disconnect_retains_snapshot_and_reconnects(monkeypatch):
    data = snapshot()
    calls = []
    async def fetch(_):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("must not expose host paths")
        return data
    monkeypatch.setattr(module, "get_snapshot", fetch)
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert "◐ STALE" in content(app, "#connection")
        assert health_color(app, "◐") == Color.parse(app.state_color("degraded"))
        assert "host paths" not in content(app, "#connection")
        assert app.query_one(DataTable).row_count == 2
        data["instance_id"] = "restarted"
        await pilot.press("r")
        await pilot.pause()
        assert "CONNECTED" in content(app, "#connection")
        assert app.snapshot["instance_id"] == "restarted"


async def test_empty_and_stale_resources(monkeypatch):
    data = snapshot(0)
    data.update(platforms=[], scheduling=None, generated_at=(module.datetime.now(UTC) - timedelta(seconds=20)).isoformat())
    data["harnesses"][0]["observed_at"] = data["generated_at"]
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "No active turns" in content(app, "#keys")
        assert "STALE" in content(app, "#connection")
        await pilot.press("3")
        assert "none configured" in content(app, "#keys")
        await pilot.press("2")
        assert "unavailable" in content(app, "#keys")
        await pilot.press("4", "enter")
        assert "20s" in " ".join(str(widget.content) for widget in app.screen.query(Static))


async def test_loading_and_disconnected_input(monkeypatch):
    import asyncio
    pending = asyncio.Event()
    async def fetch(_):
        await pending.wait()
        raise OSError("unavailable")
    monkeypatch.setattr(module, "get_snapshot", fetch)
    app = BelleApp(Path("/unused"))
    monkeypatch.setattr(app.console, "_color_system", ColorSystem.TRUECOLOR)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert "◌ CONNECTING" in content(app, "#connection")
        assert health_color(app, "◌") == Color.parse(app.state_color("degraded"))
        await pilot.press("3")
        assert app.active_view == "platforms"
        pending.set()
        await pilot.pause()
        assert "○ DISCONNECTED" in content(app, "#connection")
        assert health_color(app, "○") == Color.parse(app.state_color("error"))


def test_work_start_includes_date_for_older_execution():
    app = BelleApp(Path("/unused"))
    app.snapshot = snapshot()
    app.snapshot["generated_at"] = "2026-10-18T12:00:00Z"
    assert app.work_started("2026-10-18T11:00:00Z") == "11:00:00Z"
    assert app.work_started("2026-10-17T11:00:00Z") == "10-17 11:00:00"
    assert app.work_started(None) == "—"


def test_time_plus_formats_long_running_turns():
    assert BelleApp.format_elapsed(0) == "00:00"
    assert BelleApp.format_elapsed(3723) == "1:02:03"
    assert BelleApp.format_elapsed(None) == "—"


async def test_two_clients_follow_a_socket_restart(tmp_path):
    async def work():
        return {"id": "work", "state": "ready", "reason": None,
                "observed_at": datetime.now(UTC).isoformat(),
                "metrics": {"queued_messages": {"value": 0, "reason": None}}}

    service = StatusService(SnapshotCollector({"work": work}), tmp_path,
                            ControlSettings(enabled=True))
    assert await service.start()
    socket_path = service.socket_path
    assert socket_path is not None
    first = BelleApp(socket_path)
    second = BelleApp(socket_path)
    try:
        async with first.run_test(size=(60, 35)) as first_pilot, second.run_test(size=(80, 35)) as second_pilot:
            await first_pilot.pause()
            await second_pilot.pause()
            old_id = service.collector.instance_id
            for app in (first, second):
                assert app.snapshot["instance_id"] == old_id

            await service.stop()
            await first_pilot.press("r")
            await second_pilot.press("r")
            for app in (first, second):
                assert "STALE" in content(app, "#connection")
                assert app.snapshot["instance_id"] == old_id

            assert await service.start()
            new_id = service.collector.instance_id
            assert new_id != old_id
            await first_pilot.press("r")
            await second_pilot.press("r")
            for app in (first, second):
                assert "CONNECTED" in content(app, "#connection")
                assert app.snapshot["instance_id"] == new_id
        assert (await get_snapshot(socket_path))["instance_id"] == new_id
    finally:
        await service.stop()


async def test_header_shows_health_load_and_update_time_on_right(monkeypatch):
    data = snapshot()
    data["build"] = {"version": "1.2.3", "revision": "1234567890abcdef"}
    data["work"]["metrics"]["queued_messages"] = {"value": 3, "reason": None}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        shown = content(app, "#connection")
        assert shown.startswith("Belle")
        assert "1.2.3" not in shown and "1234567" not in shown
        assert shown.index("● CONNECTED") < shown.index("running 0") < shown.index("queued 3")
        assert shown.endswith(datetime.fromisoformat(data["generated_at"]).astimezone().strftime("%H:%M:%S"))
        assert len(shown) <= 60
        assert app.query_one("#views").region.bottom == app.query_one(DataTable).region.y
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert "1.2.3" not in content(app, "#connection")
        assert content(app, "#connection").endswith(shown[-8:])


async def test_schedule_rows_and_complete_details(monkeypatch):
    data = snapshot()
    data["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                          "observed_at": data["generated_at"], "metrics": {}, "rows": [
        {"job_id": "job-local-123456789", "name": "Daily digest", "state": "active",
         "when": "cron expr=0 9 * * * zone=Europe/Paris", "next_due": "2026-10-19T07:00:00Z"}]}
    install(monkeypatch, data)
    app = BelleApp(Path("/unused"))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        assert app.query_one(DataTable).row_count == 1
        assert "Daily digest" in str(app.query_one(DataTable).get_row_at(0))
        assert "0 9 * * * (Europe/Paris)" in str(app.query_one(DataTable).get_row_at(0))
        await pilot.press("enter")
        detail = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert "job-local-123456789" in detail
        assert "Europe/Paris" in detail and "2026-10-19T07:00:00Z" in detail


def test_cron_summary_keeps_expression_and_zone():
    assert BelleApp.schedule_summary("cron expr=0 9 * * * zone=UTC") == "0 9 * * * (UTC)"
    assert BelleApp.schedule_summary("cron zone=UTC second=30 expr=0 9 * * *") == "0 9 * * * (UTC) +30s"


@pytest.mark.parametrize("width", [60, 80, 120])
async def test_space_toggles_selected_job_and_reports_failure(monkeypatch, width):
    from belle_agent.control.protocol import ControlError

    data = snapshot()
    data["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                          "observed_at": data["generated_at"], "metrics": {}, "rows": [
                              {"job_id": "job-selected", "name": "selected", "state": "active",
                               "when": "once tomorrow", "next_due": None},
                              {"job_id": "job-other", "name": "other", "state": "ended",
                               "when": "once yesterday", "next_due": None}]}
    install(monkeypatch, data)
    calls, notices = [], []

    async def toggle(socket, job_id, expected_state, instance_id):
        calls.append((job_id, expected_state, instance_id))
        if len(calls) == 3:
            raise ControlError("no_change", "Job state changed; refresh Jobs before retrying")
        result = deepcopy(data)
        result["scheduling"]["rows"][0]["state"] = "paused" if expected_state == "active" else "active"
        return result

    monkeypatch.setattr(module, "toggle_schedule", toggle)
    app = BelleApp(Path("/unused"))
    app.poll_seconds = 60
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        assert not calls
        await pilot.press("2")
        await pilot.pause()
        assert ("Space pause" if width < 64 else "Space pause/resume") in content(app, "#keys")
        assert "Del delete" in content(app, "#keys")
        assert cell_len(content(app, "#keys")) <= width - 2
        for state in ["paused", "active"]:
            await pilot.press("space")
            await pilot.pause()
            assert column_cells(app.query_one(DataTable), "STATE")[0] == state
        await pilot.press("space")
        await pilot.pause()
        assert notices[-1][1]["severity"] == "error"
        assert "refresh Jobs" in notices[-1][0]
        assert calls == [("job-selected", state, data["instance_id"]) for state in ["active", "paused", "active"]]
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("space")
        await pilot.pause()
        assert len(calls) == 3
        assert notices[-1][1]["severity"] == "warning"


@pytest.mark.parametrize(("action", "view", "row", "method", "identity", "key"), [
    ("stop_work", "work", 0, "stop_work", "local-execution-0", "x"),
    ("drop_work", "work", 1, "drop_work", "local-execution-1", "d"),
    ("delete_job", "scheduling", 0, "delete_schedule", "job-selected", "delete"),
])
async def test_destructive_controls_confirm_cancel_and_target(monkeypatch, action, view, row,
                                                              method, identity, key):
    data = snapshot()
    data["work"]["rows"][1]["type"] = "main"
    data["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                          "observed_at": data["generated_at"], "metrics": {}, "rows": [
                              {"job_id": "job-selected", "name": "selected", "state": "active",
                               "when": "once tomorrow", "next_due": None}]}
    install(monkeypatch, data)
    calls = []

    async def control(socket, target, state, instance):
        calls.append((target, state, instance))
        return deepcopy(data)

    monkeypatch.setattr(module, method, control)
    app = BelleApp(Path("/unused"))
    app.poll_seconds = 60
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        if view == "scheduling":
            await pilot.press("2")
        else:
            assert "x stop" in content(app, "#keys")
            assert "d drop" in content(app, "#keys")
        table = app.query_one(DataTable)
        table.move_cursor(row=row)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmAction)
        assert "cannot be undone" in str(app.screen.query_one("#confirm-message", Static).content)
        assert app.focused.id == "confirm-cancel"
        assert app._background_screens
        assert app.screen.styles.background.a < 1
        if action == "stop_work":
            data["work"]["rows"].append({**data["work"]["rows"][1], "id": "new-queued"})
            await app.action_refresh().wait()
            assert len(app.snapshot["work"]["rows"]) == 3
            await pilot.pause()
            assert table.row_count == 3
            assert isinstance(app.screen, ConfirmAction)
        await pilot.press("right")
        assert app.focused.id == "confirm-yes"
        assert (app.screen.query_one("#confirm-yes").styles.background !=
                app.screen.query_one("#confirm-cancel").styles.background)
        await pilot.press("left")
        assert app.focused.id == "confirm-cancel"
        assert app.screen.query_one("#confirm-box").region.right <= 60
        assert cell_len(content(app, "#keys")) <= 58
        if action == "stop_work":
            title = str(app.screen.query_one("#confirm-title", Static).content)
            assert "slack / channel-thread-identity / local-ex…on-0" in title
        if action == "drop_work":
            await pilot.click("#confirm-cancel")
        else:
            await pilot.press("escape")
        await pilot.pause()
        assert calls == []

        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmAction)
        if action == "delete_job":
            assert "selected (job-selected)" in str(app.screen.query_one("#confirm-title", Static).content)
            await pilot.click("#confirm-yes")
        else:
            await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert calls == [(identity, "running" if action == "stop_work" else
                          "queued" if action == "drop_work" else "active", data["instance_id"])]


async def test_notifications_use_belle_surface(monkeypatch):
    install(monkeypatch, snapshot())
    app = BelleApp(Path("/unused"))
    async with app.run_test(notifications=True) as pilot:
        app.notify("Job resumed")
        await pilot.pause()
        toast = app.query_one("Toast")
        assert tuple(toast.styles.background)[:3] == tuple(Color.parse(app.get_css_variables()["surface"]).triplet)


async def test_toggle_captures_identity_and_ignores_duplicate_space(monkeypatch):
    import asyncio

    data = snapshot()
    data['scheduling'] = {'id': 'scheduling', 'state': 'ready', 'reason': None,
                          'observed_at': data['generated_at'], 'metrics': {}, 'rows': [
                              {'job_id': 'first', 'name': 'first', 'state': 'active', 'when': 'once later', 'next_due': None},
                              {'job_id': 'second', 'name': 'second', 'state': 'active', 'when': 'once later', 'next_due': None}]}
    install(monkeypatch, data)
    pending = asyncio.Event()
    calls = []

    async def toggle(socket, job_id, state, instance):
        calls.append(job_id)
        await pending.wait()
        result = deepcopy(data)
        result['scheduling']['rows'][0]['state'] = 'paused'
        return result

    monkeypatch.setattr(module, 'toggle_schedule', toggle)
    app = BelleApp(Path('/unused'))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('2', 'space')
        await pilot.pause()
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press('space')
        await pilot.pause()
        assert calls == ['first']
        pending.set()
        await app.workers.wait_for_complete()
        app.snapshot['generated_at'] = (module.datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        await pilot.press('space')
        await pilot.pause()
        assert calls == ['first']


@pytest.mark.parametrize('failed_read', [False, True])
async def test_poll_started_before_toggle_cannot_replace_action_result(monkeypatch, failed_read):
    import asyncio

    data = snapshot()
    data['scheduling'] = {'id': 'scheduling', 'state': 'ready', 'reason': None,
                          'observed_at': data['generated_at'], 'metrics': {}, 'rows': [
                              {'job_id': 'selected', 'name': 'selected', 'state': 'active',
                               'when': 'once later', 'next_due': None}]}
    pending, started = asyncio.Event(), asyncio.Event()
    reads = 0

    async def fetch(socket):
        nonlocal reads
        reads += 1
        if reads > 1:
            started.set()
            await pending.wait()
            if failed_read:
                raise OSError('late failed poll')
        return data

    async def toggle(*args):
        result = deepcopy(data)
        result['scheduling']['rows'][0]['state'] = 'paused'
        return result

    monkeypatch.setattr(module, 'get_snapshot', fetch)
    monkeypatch.setattr(module, 'toggle_schedule', toggle)
    app = BelleApp(Path('/unused'))
    app.poll_seconds = 60
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('2', 'r')
        await started.wait()
        await pilot.press('space')
        await pilot.pause()
        assert app.snapshot['scheduling']['rows'][0]['state'] == 'paused'
        pending.set()
        await app.workers.wait_for_complete()
        assert app.snapshot['scheduling']['rows'][0]['state'] == 'paused'
        assert app.failure is None
