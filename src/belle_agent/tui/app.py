"""Resource browser with independent polling and an explicit scheduler toggle."""

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from rich.cells import cell_len
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.events import Click, Key, MouseScrollDown, MouseScrollUp, Resize
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static, Tab, Tabs

from belle_agent.control.client import (
    delete_schedule,
    drop_work,
    get_snapshot,
    peek_close,
    peek_open,
    peek_poll,
    stop_work,
    toggle_schedule,
)
from belle_agent.control.protocol import ControlError, Snapshot
from belle_agent.tui.peek_format import (
    PEEK_KIND_OUTCOME,
    PEEK_KIND_QUESTION,
    PEEK_KIND_STEP,
    PEEK_KIND_TEXT,
    PEEK_KIND_THINKING,
    PEEK_KIND_TOOL_CALL,
    PEEK_KIND_TOOL_RESULT,
    PEEK_KIND_USAGE,
    PeekKind,
    PeekPart,
    format_peek_question,
    format_peek_step,
    format_peek_tool_body_marker,
    format_peek_tool_call,
    format_peek_tool_result,
    format_peek_usage,
    peek_tool_body,
    render_peek_parts,
)

_PEEK_HINTS = "Esc close · +/= expand · - collapse · scrollback is the in-memory ring"


def elapsed_seconds(timestamp: str, now: datetime | None = None) -> int:
    return max(0, int(((now or datetime.now(UTC)) - datetime.fromisoformat(timestamp)).total_seconds()))


def age(timestamp: str | None) -> str:
    """Distinguish unknown freshness from a measured age."""
    return f"{elapsed_seconds(timestamp)}s" if timestamp else "unknown"


def format_peek_duration(seconds: float) -> str:
    """Compact duration for a peek end line."""
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_peek_outcome(payload: dict[str, Any]) -> str:
    """Build the dim end line: Worked for xx - hh:mm - extra."""
    duration = payload.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        worked = f"Worked for {format_peek_duration(float(duration))}"
    else:
        worked = "Worked for unknown time"
    parts = [worked]
    ended_at = payload.get("ended_at")
    if isinstance(ended_at, str) and ended_at:
        try:
            parts.append(datetime.fromisoformat(ended_at).strftime("%H:%M"))
        except ValueError:
            pass
    extra = payload.get("extra")
    if isinstance(extra, str) and extra.strip():
        parts.append(extra.strip())
    return " - ".join(parts)


#: Nested tool-body scroll height so one expand does not shove the outer peek stream.
PEEK_TOOL_BODY_MAX_HEIGHT = 8


class PeekToolRow(Vertical):
    """Collapsed tool meta line with optional nested, height-capped body scroll."""

    DEFAULT_CSS = f"""
    PeekToolRow {{ height: auto; margin: 0; padding: 0; }}
    PeekToolRow > #tool-meta {{ height: 1; margin: 0; padding: 0; }}
    PeekToolRow > #tool-body-row {{
        display: none;
        height: auto;
        margin: 0;
        padding: 0;
    }}
    PeekToolRow.-expanded > #tool-body-row {{ display: block; }}
    PeekToolRow > #tool-body-row > #tool-marker {{
        width: auto;
        height: auto;
        margin: 0;
        padding: 0;
    }}
    PeekToolRow > #tool-body-row > #tool-body-scroll {{
        width: 1fr;
        height: auto;
        max-height: {PEEK_TOOL_BODY_MAX_HEIGHT};
        margin: 0;
        padding: 0;
        background: $panel;
    }}
    PeekToolRow > #tool-body-row > #tool-body-scroll > Static {{
        height: auto; margin: 0; padding: 0;
    }}
    """

    def __init__(self, kind: PeekKind, payload: dict[str, Any]) -> None:
        super().__init__()
        self.kind = kind
        self.payload = payload
        self.body = peek_tool_body(payload, kind)
        self.expanded = False
        self.can_focus = self.body is not None

    def compose(self) -> ComposeResult:
        yield Static(self._meta_line(), id="tool-meta", markup=False)
        with Horizontal(id="tool-body-row"):
            yield Static(id="tool-marker", markup=False)
            with VerticalScroll(id="tool-body-scroll"):
                yield Static(self.body or "", id="tool-body", markup=False)

    def _meta_line(self) -> str:
        # No trailing newline: a blank row between meta and payload is wrong.
        if self.kind == PEEK_KIND_TOOL_CALL:
            return format_peek_tool_call(self.payload, expanded=self.expanded).rstrip("\n")
        return format_peek_tool_result(self.payload, expanded=self.expanded).rstrip("\n")

    def _marker_renderable(self) -> Text:
        accent = self.app.get_css_variables().get("accent", "#d4a04a")
        return format_peek_tool_body_marker(marker_style=f"bold {accent}")

    def _sync(self) -> None:
        self.query_one("#tool-meta", Static).update(self._meta_line())
        if self.expanded and self.body is not None:
            self.query_one("#tool-marker", Static).update(self._marker_renderable())
        self.set_class(self.expanded, "-expanded")

    def toggle(self) -> None:
        if self.body is None:
            return
        self.expanded = not self.expanded
        self._sync()

    def on_click(self, _event: Click) -> None:
        self.toggle()

    def on_key(self, event: Key) -> None:
        if self.body is None:
            return
        if event.key in {"plus", "equals_sign", "enter"}:
            if not self.expanded:
                self.toggle()
            event.stop()
        elif event.key in {"minus", "underscore"} and self.expanded:
            self.toggle()
            event.stop()


class Details(ModalScreen[None]):
    """Full values for the selected resource, without table truncation."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss", "Close"), ("enter", "dismiss", "Close")]
    DEFAULT_CSS = """
    Details { align: center middle; background: $background 70%; }
    #detail-body { width: 90%; max-width: 100; height: 80%; padding: 1 2;
                   border: solid $accent; background: $surface; }
    #detail-body Static { height: auto; }
    """

    def __init__(self, title: str, fields: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail-body"):
            yield Static(Text(self.title_text, style="bold"))
            label_width = max((len(key) for key, _ in self.fields), default=0)
            yield Static(Text("\n".join(f"{key:<{label_width}}  {value}" for key, value in self.fields)))
            yield Static("\nEsc / Enter: close", markup=False)


class PeekScreen(ModalScreen[None]):
    """Live peek of a running main or subagent Activity row."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close_peek", "Close"),
        ("plus", "expand_tool", "Expand tool"),
        ("equals_sign", "expand_tool", "Expand tool"),
        ("minus", "collapse_tool", "Collapse tool"),
    ]
    DEFAULT_CSS = """
    PeekScreen { align: center middle; background: $background 70%; }
    #peek-box { width: 90%; max-width: 120; height: 80%; padding: 1 2;
                border: solid $accent; background: $surface; }
    #peek-title { height: 1; text-style: bold; color: $accent; margin-bottom: 1; }
    #peek-scroll { height: 1fr; }
    #peek-stream { height: auto; }
    #peek-stream > Static { height: auto; }
    #peek-hints { height: 1; color: $text-muted; }
    """

    def __init__(
        self, socket: Path, work_id: str, instance_id: str, *, label: str | None = None,
    ) -> None:
        super().__init__()
        self.socket = socket
        self.work_id = work_id
        self.instance_id = instance_id
        self.label = label
        self.peek_id: str | None = None
        self.after_seq = 0
        self._ended = False
        self._polling = False
        self._follow = True
        self._poll_error: str | None = None
        self._waiting: Static | None = None
        self._open_static: Static | None = None
        self._open_kind: PeekKind | None = None
        self._open_buf = ""
        self._widgets: list[Static | PeekToolRow] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="peek-box"):
            yield Static(self._title(), id="peek-title", markup=False)
            with VerticalScroll(id="peek-scroll"):  # noqa: SIM117
                with Vertical(id="peek-stream"):
                    self._waiting = Static("Opening…", markup=False)
                    yield self._waiting
            yield Static(_PEEK_HINTS, id="peek-hints", markup=False)

    def _title(self, suffix: str = "") -> str:
        who = self.label.strip() if isinstance(self.label, str) and self.label.strip() else self.work_id[:12]
        return f"Peek · {who}{suffix}"

    def _stream(self) -> Vertical:
        return self.query_one("#peek-stream", Vertical)

    def on_mount(self) -> None:
        self.set_interval(0.35, self._tick)
        self.query_one("#peek-scroll", VerticalScroll).focus()
        self._open()

    def on_mouse_scroll_up(self, _event: MouseScrollUp) -> None:
        self._follow = False

    def on_mouse_scroll_down(self, _event: MouseScrollDown) -> None:
        self.call_after_refresh(self._sync_follow)

    def on_key(self, event: Key) -> None:
        if event.key in {"up", "pageup", "home", "ctrl+home"}:
            self._follow = False
        elif event.key in {"down", "pagedown", "end", "ctrl+end"}:
            self.call_after_refresh(self._sync_follow)

    @work(exclusive=True, group="peek-open")
    async def _open(self) -> None:
        # Shield so Esc (screen dismiss cancels this worker) cannot cancel the
        # socket open; the server may still allocate a subscriber we must close.
        open_task = asyncio.create_task(
            peek_open(self.socket, self.work_id, self.instance_id),
        )
        try:
            opened = await asyncio.shield(open_task)
        except asyncio.CancelledError:
            self._reclaim_open_task(open_task)
            raise
        except ControlError as exc:
            if not self._ended:
                self._set_status(f"{exc.code}: {exc.message}")
                self._ended = True
            return
        except (OSError, TimeoutError):
            if not self._ended:
                self._set_status("connection unavailable")
                self._ended = True
            return
        peek_id = opened["peek_id"]
        # Esc during open dismisses before peek_id is set; close the late ID.
        if self._ended:
            await self._close_peek_id(peek_id)
            return
        self.peek_id = peek_id
        self._clear_waiting()
        self._note_lost(int(opened.get("lost") or 0))
        self._append(opened["frames"])
        if not opened["live"]:
            self._mark_ended()
            if not self._widgets:
                self._set_status("(ended with no text)")
        elif not self._widgets:
            self._set_status("(waiting for text…)")

    def _reclaim_open_task(self, open_task: asyncio.Task[dict[str, Any]]) -> None:
        """After Esc cancels the open worker, close any subscriber the server still returns."""
        socket = self.socket

        async def _close_when_ready() -> None:
            try:
                opened = await open_task
            except (ControlError, OSError, TimeoutError, asyncio.CancelledError):
                return
            await self._close_peek_id(opened["peek_id"], socket=socket)

        # App-scoped: the PeekScreen worker is already cancelled/dismissed.
        self.app.run_worker(_close_when_ready(), group="peek-close", exclusive=False)

    async def _close_peek_id(
        self, peek_id: str, *, socket: Path | None = None,
    ) -> None:
        try:
            await peek_close(socket or self.socket, peek_id)
        except (ControlError, OSError, TimeoutError):
            pass

    @work(group="peek-poll")
    async def _tick(self) -> None:
        if self._ended or self.peek_id is None or self._polling:
            return
        self._polling = True
        try:
            polled = await peek_poll(self.socket, self.peek_id, self.after_seq)
            self._clear_poll_error()
            self._note_lost(int(polled.get("lost") or 0))
            self._append(polled["frames"])
            if not polled["live"]:
                self._mark_ended()
        except ControlError as exc:
            if exc.code == "peek_ended":
                self._mark_ended()
            else:
                self._show_poll_error(f"{exc.code}: {exc.message}")
        except (OSError, TimeoutError):
            self._show_poll_error("connection unavailable")
        finally:
            self._polling = False

    def _mark_ended(self, state: str = "ended") -> None:
        if self._ended:
            return
        self._ended = True
        self._flush_open_static()
        self.query_one("#peek-title", Static).update(self._title(f" · {state}"))
        peek_id = self.peek_id
        self.peek_id = None
        if peek_id is not None:
            self._release_peek(peek_id)

    @work(exclusive=True, group="peek-close")
    async def _release_peek(self, peek_id: str) -> None:
        try:
            await peek_close(self.socket, peek_id)
        except (ControlError, OSError, TimeoutError):
            pass

    def _sync_follow(self) -> None:
        scroll = self.query_one("#peek-scroll", VerticalScroll)
        self._follow = scroll.max_scroll_y <= 0 or scroll.scroll_y >= scroll.max_scroll_y - 1

    def _clear_waiting(self) -> None:
        if self._waiting is not None:
            self._waiting.remove()
            self._waiting = None

    def _set_status(self, message: str) -> None:
        """Replace the stream with a terminal status (open failure / empty wait only)."""
        stream = self._stream()
        for child in list(stream.children):
            child.remove()
        self._widgets.clear()
        self._open_static = None
        self._open_kind = None
        self._open_buf = ""
        self._waiting = Static(message, markup=False)
        stream.mount(self._waiting)

    def _show_poll_error(self, message: str) -> None:
        self._poll_error = message
        self.query_one("#peek-hints", Static).update(f"poll error: {message} · retrying…")

    def _clear_poll_error(self) -> None:
        if self._poll_error is None:
            return
        self._poll_error = None
        self.query_one("#peek-hints", Static).update(_PEEK_HINTS)

    def _note_lost(self, lost: int) -> None:
        if lost <= 0:
            return
        self._clear_waiting()
        label = "frame" if lost == 1 else "frames"
        self._append_stream_part(PEEK_KIND_STEP, f"\n· ── {lost} {label} omitted ──\n")

    def _append(self, frames: list[dict[str, Any]]) -> None:
        if not frames:
            return
        self._clear_waiting()
        for frame in frames:
            self.after_seq = max(self.after_seq, frame["seq"])
            kind = frame["kind"]
            payload = frame["payload"]
            if kind == PEEK_KIND_THINKING:
                self._append_stream_part(PEEK_KIND_THINKING, str(payload.get("text") or ""))
            elif kind == PEEK_KIND_TEXT:
                self._append_stream_part(PEEK_KIND_TEXT, str(payload.get("text") or ""))
            elif kind == PEEK_KIND_STEP:
                self._append_stream_part(PEEK_KIND_STEP, format_peek_step(payload))
            elif kind == PEEK_KIND_TOOL_CALL:
                self._append_tool(PEEK_KIND_TOOL_CALL, payload)
            elif kind == PEEK_KIND_TOOL_RESULT:
                self._append_tool(PEEK_KIND_TOOL_RESULT, payload)
            elif kind == PEEK_KIND_QUESTION:
                self._append_stream_part(PEEK_KIND_QUESTION, format_peek_question(payload))
            elif kind == PEEK_KIND_USAGE:
                line = format_peek_usage(payload)
                if line:
                    self._append_stream_part(PEEK_KIND_USAGE, line)
            elif kind == PEEK_KIND_OUTCOME:
                state = str(payload.get("state") or "")
                self._append_stream_part(
                    PEEK_KIND_OUTCOME, f"\n\n{format_peek_outcome(payload)}\n",
                )
                self._mark_ended(state or "ended")
        if self._follow:
            self.query_one("#peek-scroll", VerticalScroll).scroll_end(animate=False)

    def _append_stream_part(self, kind: PeekKind, text: str) -> None:
        if not text:
            return
        # Keep one open Static across polls so mid-paragraph text stays plain.
        if self._open_static is not None and self._open_kind == kind:
            self._open_buf += text
            self._open_static.update(
                render_peek_parts([PeekPart(kind, self._open_buf)], flush=False),
            )
            return
        self._flush_open_static()
        self._open_buf = text
        widget = Static(render_peek_parts([PeekPart(kind, text)], flush=False), markup=False)
        self._widgets.append(widget)
        self._stream().mount(widget)
        self._open_static = widget
        self._open_kind = kind

    def _append_tool(self, kind: PeekKind, payload: dict[str, Any]) -> None:
        self._flush_open_static()
        row = PeekToolRow(kind, payload)
        self._widgets.append(row)
        self._stream().mount(row)

    def _flush_open_static(self) -> None:
        if self._open_static is None or self._open_kind is None:
            return
        self._open_static.update(
            render_peek_parts([PeekPart(self._open_kind, self._open_buf)], flush=True),
        )
        self._open_static = None
        self._open_kind = None
        self._open_buf = ""

    def action_expand_tool(self) -> None:
        row = self._focused_tool_row()
        if row is None or row.body is None:
            return
        if not row.expanded:
            row.toggle()

    def action_collapse_tool(self) -> None:
        row = self._focused_tool_row()
        if row is not None and row.expanded:
            row.toggle()

    def _focused_tool_row(self) -> PeekToolRow | None:
        focused = self.focused
        if isinstance(focused, PeekToolRow):
            return focused
        if focused is not None:
            row = focused.parent
            while row is not None:
                if isinstance(row, PeekToolRow):
                    return row
                row = row.parent
        rows = list(self._stream().query(PeekToolRow))
        return rows[-1] if rows else None

    async def action_close_peek(self) -> None:
        peek_id = self.peek_id
        self.peek_id = None
        self._ended = True
        if peek_id is not None:
            try:
                await peek_close(self.socket, peek_id)
            except (ControlError, OSError, TimeoutError):
                pass
        self.dismiss()


class ConfirmAction(ModalScreen[bool]):
    """A compact, keyboard-first confirmation for destructive controls."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"), ("n", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("left", "focus_cancel", "Cancel"), ("right", "focus_confirm", "Confirm"),
    ]
    DEFAULT_CSS = """
    ConfirmAction { align: center middle; }
    #confirm-box { width: 80%; max-width: 72; min-width: 36; height: auto;
                  padding: 1 2; border: round $error; background: $surface; }
    #confirm-title { height: auto; color: $error; text-style: bold; }
    #confirm-message { height: auto; margin: 1 0; }
    #confirm-hints { height: auto; color: $text-muted; }
    #confirm-buttons { height: 3; align-horizontal: right; }
    #confirm-buttons Button { min-width: 12; margin-left: 1; }
    #confirm-buttons Button { background: $panel; color: $text; }
    #confirm-yes { color: $error; }
    #confirm-buttons Button:focus { background: $accent; color: auto; text-style: bold; }
    """

    def __init__(self, title: str, message: str, confirm_label: str) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Button

        with Vertical(id="confirm-box"):
            yield Static(self.title_text, id="confirm-title", markup=False)
            yield Static(self.message, id="confirm-message", markup=False)
            yield Static("←/→ choose · Enter select · Esc cancel", id="confirm-hints", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(self.confirm_label, id="confirm-yes")

    def on_mount(self) -> None:
        self.query_one("#confirm-cancel").focus()

    def on_button_pressed(self, event) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_focus_cancel(self) -> None:
        self.query_one("#confirm-cancel").focus()

    def action_focus_confirm(self) -> None:
        self.query_one("#confirm-yes").focus()


class ResourceTable(DataTable[Text]):
    def _should_highlight(self, cursor: Coordinate, target_cell: Coordinate,
                          type_of_cursor: Literal["cell", "row", "column", "none"]) -> bool:
        # Textual applies row hover to every header cell in row-cursor tables.
        if cursor.row == target_cell.row == -1:
            return cursor == target_cell
        return super()._should_highlight(cursor, target_cell, type_of_cursor)


class BelleApp(App[None]):
    """Browse status resources and pause or resume selected scheduler jobs."""

    TITLE = "Belle"
    BRAND_BURST_INTERVAL_SECONDS = 10
    BRAND_BURST_FRAMES = 27
    BRAND_BURST_FRAME_SECONDS = 0.095
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        ("1", "view('work')", "Activity"), ("2", "view('scheduling')", "Jobs"),
        ("3", "view('platforms')", "Platforms"), ("4", "view('harnesses')", "Harnesses"),
        ("5", "view('models')", "Models"),
        ("r", "refresh", "Refresh"), ("q", "quit", "Quit"),
        ("c", "sort_column", "Column"), ("s", "sort", "Sort"),
        ("t", "toggle_tree", "Tree"), ("p", "peek_work", "Peek output"),
        ("x", "stop_work", "Stop activity"),
        ("d", "drop_work", "Drop queued"), ("delete", "delete_job", "Delete job"),
        ("space", "toggle_job", "Pause/resume"),
    ]
    CSS = """
    Screen { background: $background; }
    ConfirmAction { background: $background 35%; }
    #connection { height: 1; padding: 0 1; }
    #views { height: 1; }
    #views Underline { display: none; }
    #views Tab { padding: 0; background: $surface; }
    #views Tab.-active { color: $accent; text-style: bold; background: $surface; }
    #views:focus Tab.-active { color: $accent; text-style: bold; background: $surface; }
    #resources { height: 1fr; border: none; }
    #keys { height: 1; padding: 0 1; }
    #connection, #views, #keys { background: $surface; }
    DataTable:focus { background-tint: transparent; }
    DataTable > .datatable--header { background: $accent; color: auto; text-style: bold; }
    DataTable:focus > .datatable--header { background: $accent; background-tint: transparent; }
    DataTable > .datatable--cursor { background: $accent 25%; color: $text; }
    DataTable:focus > .datatable--cursor { background: $accent 25%; color: $text; }
    Toast { background: $surface; color: $text; }
    Toast.-information { border-left: outer $accent; }
    Toast.-error { border-left: outer $error; }
    Toast.-warning { border-left: outer $warning; }
    """

    def __init__(self, socket: Path, *, theme: str = "dark") -> None:
        super().__init__()
        self.socket = socket
        self.selected_theme = theme
        self.snapshot: Snapshot | None = None
        self.failure: str | None = None
        self._brand_burst_frame = -1
        self._brand_burst_timer = None
        self._refreshing = False
        self._toggling = False
        self._acting = False
        self._toggle_generation = 0
        self.poll_seconds = 2.0
        self.active_view = "work"
        self.work_tree = False
        self._table_signature: object = None
        self._details: list[list[tuple[str, str]]] = []
        self._columns: list[tuple[str, str, int]] = []
        self._row_ids: list[str] = []
        self._sort: dict[str, tuple[str, bool]] = dict.fromkeys(
            ("platforms", "harnesses", "scheduling", "work"), ("state", False))
        self._sort["models"] = ("role", False)
        self._sort_target: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("Belle | ◌ CONNECTING | running —  queued — | update unknown", id="connection", markup=False)
        yield Tabs(Tab(Text.assemble(("[1]", "dim"), " Activity"), id="work"),
                   Tab(Text.assemble((" [2]", "dim"), " Jobs"), id="scheduling"),
                   Tab(Text.assemble((" [3]", "dim"), " Platforms"), id="platforms"),
                   Tab(Text.assemble((" [4]", "dim"), " Harnesses"), id="harnesses"),
                   Tab(Text.assemble((" [5]", "dim"), " Models"), id="models"),
                   active="work", id="views")
        yield ResourceTable(id="resources", cursor_type="row", show_row_labels=False, cell_padding=1)
        yield Static(id="keys")

    def on_mount(self) -> None:
        """Start polling without blocking input."""
        base = self.get_theme(f"textual-{self.selected_theme}")
        assert base is not None
        low_color = self.console.color_system in {"standard", "windows"}
        accent = ("yellow" if low_color else
                  "#8a5a10" if self.selected_theme == "light" else "#d4a04a")
        palette = {"accent": accent}
        if self.selected_theme == "dark" and not low_color:
            palette.update(foreground="#e8eef4", background="#0f1419",
                           surface="#1a222c", panel="#243040")
        self.register_theme(replace(base, name=f"belle-{self.selected_theme}", **palette))
        self.theme = f"belle-{self.selected_theme}"
        self.query_one("#keys", Static).update(self.key_hints(empty="Waiting for snapshot"))
        self.query_one(DataTable).focus()
        self.set_interval(self.poll_seconds, self.action_refresh)
        self.set_interval(1, self.render_snapshot)
        self.set_interval(self.BRAND_BURST_INTERVAL_SECONDS, self._start_brand_burst)
        self.action_refresh()

    def on_click(self, event: Click) -> None:
        header = self.query_one("#connection", Static)
        if event.widget is not header or event.button != 1:
            return
        offset = event.get_content_offset(header)
        brand_x = header.content.plain.index("Belle")
        if offset is not None and brand_x <= offset.x < brand_x + 5:
            self._start_brand_burst()

    def _start_brand_burst(self) -> None:
        if (self.console.color_system is None or os.environ.get("NO_COLOR") or
                os.environ.get("TEXTUAL_ANIMATIONS", "").lower() == "none"):
            return
        if self._brand_burst_timer is not None:
            self._brand_burst_timer.stop()
        self._brand_burst_frame = 0
        self.render_header()
        self._brand_burst_timer = self.set_timer(self.BRAND_BURST_FRAME_SECONDS, self._advance_brand_burst)

    def _advance_brand_burst(self) -> None:
        self._brand_burst_frame += 1
        if self._brand_burst_frame == self.BRAND_BURST_FRAMES:
            self._brand_burst_frame = -1
        self.render_header()
        if self._brand_burst_frame >= 0:
            self._brand_burst_timer = self.set_timer(self.BRAND_BURST_FRAME_SECONDS, self._advance_brand_burst)
        else:
            self._brand_burst_timer = None

    def on_resize(self, event: Resize) -> None:
        """Adapt columns while retaining the selected resource."""
        gap = 1 if event.size.width >= 64 else 0
        for tab in self.query(Tab):
            tab.styles.margin = (0, gap if tab.id != "models" else 0, 0, 0)
        if self.query("#resources"):
            self.call_after_refresh(self.render_snapshot)

    def action_view(self, name: str) -> None:
        """Choose a resource directly from any table row."""
        self.query_one(Tabs).active = name
        self.active_view = name
        self.render_snapshot()
        self.query_one(DataTable).focus()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        self.active_view = event.tab.id or "work"
        self.render_snapshot()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < len(self._details):
            self.push_screen(Details(self.active_view.replace("work", "activity").capitalize(),
                                     self._details[event.cursor_row]))

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.column_index < len(self._columns):
            self.cycle_sort(self._columns[event.column_index][1])

    def action_sort_column(self) -> None:
        """Choose a column without using the mouse."""
        if not self._columns:
            return
        keys = [key for _, key, _ in self._columns]
        current = self._sort_target.get(self.active_view, keys[0])
        self._sort_target[self.active_view] = keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]
        self.render_snapshot()

    def action_sort(self) -> None:
        if self._columns:
            self.cycle_sort(self._sort_target.get(self.active_view, self._columns[0][1]))

    def action_toggle_tree(self) -> None:
        if self.active_view == "work":
            self.work_tree = not self.work_tree
            self.render_snapshot()

    @work(group="schedule-toggle")
    async def action_toggle_job(self) -> None:
        """Capture row identity before yielding; never infer a job from its position."""
        if self.active_view != "scheduling" or self._toggling or self._acting or not isinstance(self.focused, DataTable):
            return
        if self.snapshot is None or self.failure or elapsed_seconds(self.snapshot["generated_at"]) > self.poll_seconds * 2:
            self.notify("Jobs snapshot is stale; refresh before changing a job", severity="warning")
            return
        selected = self.query_one(DataTable).cursor_row
        job_id = self._row_ids[selected] if 0 <= selected < len(self._row_ids) else None
        rows = self.snapshot["scheduling"].get("rows", [])
        job = next((row for row in rows if row.get("job_id") == job_id), None)
        if job_id is None or job is None or job["state"] not in {"active", "paused"}:
            self.notify("Select an active or paused scheduled job", severity="warning")
            return
        self._toggling = True
        self._acting = True
        self._toggle_generation += 1
        try:
            result = await toggle_schedule(self.socket, job_id, job["state"], self.snapshot["instance_id"])
            self.snapshot = result
            self.failure = None
            self.render_snapshot()
            self.notify(f"Job {job_id} {'paused' if job['state'] == 'active' else 'resumed'}")
        except (ControlError, OSError, TimeoutError) as exc:
            message = exc.message if isinstance(exc, ControlError) else "Connection failed; refresh Jobs before retrying"
            self.notify(message, severity="error")
        finally:
            self._toggling = False
            self._acting = False

    def _selected_record(self, section: str, identity_key: str) -> dict[str, Any] | None:
        if self.active_view != section:
            self.notify(f"Switch to {section.title()} to change this item", severity="warning")
            return None
        if (self.snapshot is None or self.failure or
                elapsed_seconds(self.snapshot["generated_at"]) > self.poll_seconds * 2):
            self.notify(f"{section.title()} snapshot is stale; refresh before changing an item", severity="warning")
            return None
        table = self.query_one(DataTable)
        row = table.cursor_row
        identity = self._row_ids[row] if 0 <= row < len(self._row_ids) else None
        records = self.snapshot["work" if section == "work" else "scheduling"].get("rows", [])
        return next((item for item in records if item.get(identity_key) == identity), None)

    async def _confirm_control(self, action: str, target: str, label: str) -> bool:
        self._acting = True
        try:
            confirmed = await self.push_screen_wait(ConfirmAction(
                f"{action} {target}?", f"This will {action.lower()} {target}. This cannot be undone.", label))
        except BaseException:
            self._acting = False
            raise
        if not confirmed:
            self._acting = False
        return confirmed

    @staticmethod
    def _work_target(item: dict[str, Any], noun: str) -> str:
        identity = item["id"]
        short_id = identity if len(identity) <= 16 else f"{identity[:8]}…{identity[-4:]}"
        platform = item.get("platform") or "unknown platform"
        conversation = item.get("conversation") or "unknown conversation"
        return f"{noun} ({platform} / {conversation} / {short_id})"

    def action_peek_work(self) -> None:
        """Open a live peek for the selected running main or subagent Activity row."""
        if self.active_view != "work" or self.snapshot is None:
            return
        item = self._selected_record("work", "id")
        if item is None:
            self.notify("Select a running main or subagent to peek", severity="warning")
            return
        row_type = item.get("type")
        if row_type not in {"main", "subagent"} or item.get("state") != "running":
            self.notify("Peek needs a running main or subagent", severity="warning")
            return
        label = item.get("agent") if row_type == "subagent" else None
        if not isinstance(label, str) or not label.strip():
            label = None
        self.push_screen(
            PeekScreen(self.socket, item["id"], self.snapshot["instance_id"], label=label),
        )

    @work(group="work-control")
    async def action_stop_work(self) -> None:
        if self._acting or not isinstance(self.focused, DataTable):
            return
        item = self._selected_record("work", "id")
        if item is None:
            return
        if item.get("type") != "main" or item.get("state") != "running":
            self.notify("Select a running activity turn", severity="warning")
            return
        work_id, expected_state, instance_id = item["id"], item["state"], self.snapshot["instance_id"]
        target = self._work_target(item, "activity turn")
        if not await self._confirm_control("Stop", target, "Stop activity"):
            return
        await self._run_control(stop_work, work_id, expected_state, instance_id, f"Stopped {target}")

    @work(group="work-control")
    async def action_drop_work(self) -> None:
        if self._acting or not isinstance(self.focused, DataTable):
            return
        item = self._selected_record("work", "id")
        if item is None:
            return
        if item.get("type") != "main" or item.get("state") != "queued":
            self.notify("Select a queued message", severity="warning")
            return
        work_id, expected_state, instance_id = item["id"], item["state"], self.snapshot["instance_id"]
        target = self._work_target(item, "queued message")
        if not await self._confirm_control("Drop", target, "Drop message"):
            return
        await self._run_control(drop_work, work_id, expected_state, instance_id, f"Dropped {target}")

    @work(group="schedule-delete")
    async def action_delete_job(self) -> None:
        if self._acting or not isinstance(self.focused, DataTable):
            return
        item = self._selected_record("scheduling", "job_id")
        if item is None:
            return
        job_id, expected_state, instance_id = item["job_id"], item["state"], self.snapshot["instance_id"]
        target = f"scheduled job {item.get('name') or job_id} ({job_id})"
        if not await self._confirm_control("Delete", target, "Delete job"):
            return
        await self._run_control(delete_schedule, job_id, expected_state, instance_id, f"Deleted {target}")

    async def _run_control(self, operation, identity: str, expected_state: str,
                           instance_id: str, success: str) -> None:
        self._acting = True
        try:
            self.snapshot = await operation(self.socket, identity, expected_state, instance_id)
            self.failure = None
            self.render_snapshot()
            self.notify(success)
        except (ControlError, OSError, TimeoutError) as exc:
            message = exc.message if isinstance(exc, ControlError) else "Connection failed; refresh before retrying"
            self.notify(message, severity="error")
        finally:
            self._acting = False

    def cycle_sort(self, key: str) -> None:
        """Cycle ascending, descending, and source order."""
        current = self._sort.get(self.active_view)
        self._sort_target[self.active_view] = key
        if current == (key, False):
            self._sort[self.active_view] = (key, True)
        elif current == (key, True):
            self._sort.pop(self.active_view)
        else:
            self._sort[self.active_view] = (key, False)
        self.render_snapshot()

    @work(group="snapshot")
    async def action_refresh(self) -> None:
        """Retain the last valid snapshot when a read fails."""
        if self._refreshing or (self._acting and not isinstance(self.screen, ConfirmAction)):
            return
        self._refreshing = True
        generation = self._toggle_generation
        try:
            result = await get_snapshot(self.socket)
            if generation == self._toggle_generation:
                self.snapshot = result
                self.failure = None
        except ControlError as exc:
            if generation == self._toggle_generation:
                self.failure = exc.code
        except (OSError, TimeoutError):
            if generation == self._toggle_generation:
                self.failure = "connection unavailable"
        finally:
            self._refreshing = False
        self.render_snapshot()

    def render_header(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            state = "disconnected" if self.failure else "connecting"
            self.query_one("#connection", Static).update(self.status_header("—", "—", state, "update unknown"))
            return
        snapshot_age = age(snapshot["generated_at"])
        stale = self.failure is not None or int(snapshot_age[:-1]) > self.poll_seconds * 2
        state = "stale" if stale else "connected"
        updated = datetime.fromisoformat(snapshot["generated_at"]).astimezone().strftime("%H:%M:%S")
        metrics = snapshot["work"]["metrics"]
        running = self.value(metrics["active_turns"]["value"] if "active_turns" in metrics else None)
        queued = self.value(metrics["queued_messages"]["value"] if "queued_messages" in metrics else None)
        self.query_one("#connection", Static).update(self.status_header(running, queued, state, updated))

    def render_snapshot(self) -> None:
        """Update freshness; rebuild rows only when values or columns change."""
        self.render_header()
        snapshot = self.snapshot
        if snapshot is None:
            return
        records: list[dict[str, Any]]
        if self.active_view == "work" and "rows" in snapshot["work"]:
            records = [dict(row) for row in snapshot["work"]["rows"]]
            now = datetime.now(UTC)
            for record in records:
                started = record.get("started_at")
                record["elapsed_seconds"] = elapsed_seconds(started, now) if started else None
            columns = [("STATE", "state", 9), ("TYPE", "type", 8), ("AGENT", "agent", 9),
                       ("PLATFORM", "platform", 9), ("CONVERSATION", "conversation", 10)]
            if self.size.width >= 80:
                columns += [("LOCAL ID", "id", 9), ("HARNESS", "harness", 9)]
            if self.size.width >= 100:
                if not self.work_tree:
                    columns += [("PARENT", "parent", 9)]
                columns += [("TIME+", "elapsed_seconds", 8)]
            if self.size.width >= 120:
                columns += [("MODEL", "model", 14)]
            if self.size.width >= 140:
                columns += [("IN", "tokens_in", 7), ("OUT", "tokens_out", 7)]
            if self.size.width >= 160:
                columns += [("START UTC", "started_at", 12)]
            details = [[(key.replace("_", " ").upper(), self.value(record.get(key))) for key in
                        ("state", "type", "agent", "id", "parent", "platform", "workspace", "conversation",
                         "harness", "started_at", "model",
                         "tokens_in", "tokens_out")] + [("TIME+", self.format_elapsed(record["elapsed_seconds"]))]
                       for record in records]
            empty = "No active turns or queued messages"
        elif self.active_view == "models":
            records = [{**row, "owner": "Belle"} for row in snapshot["task_models"]]
            if models := snapshot.get("harness_models"):
                records.extend({"owner": row["owner"], "role": "harness", "id": "—",
                                "model_name": row["model_name"], "api": "—", "selected": None}
                               for row in models)
            wide = self.size.width >= 80
            show_api = self.size.width >= 90
            model_width = max(10, self.size.width - (72 if show_api else 54 if wide else 46))
            columns = [("OWNER", "owner", 12), ("ROLE", "role", 10),
                       ("ENTRY", "id", 14 if wide else 6),
                       ("MODEL", "model_name", model_width)]
            if show_api:
                columns.append(("API", "api", 16))
            columns.append(("SELECTED", "selected", 8))
            details = [[("OWNER", row["owner"]), ("ROLE", row["role"]), ("ENTRY", row["id"]),
                        ("MODEL", self.value(row["model_name"])), ("API", row["api"]),
                        ("SELECTED", "yes" if row["selected"] is True else
                         "no" if row["selected"] is False else "—")]
                       for row in records]
            catalog_unavailable = snapshot.get("harness_models") is None
            empty = ("No models listed; harness model list unavailable"
                     if catalog_unavailable else "No models listed")
        elif self.active_view == "scheduling" and snapshot.get("scheduling") and "rows" in snapshot["scheduling"]:
            records = [dict(row) for row in snapshot["scheduling"]["rows"]]
            columns = [("STATE", "state", 7), ("JOB", "name", 14), ("SCHEDULE", "when", 28)]
            if self.size.width >= 80:
                columns += [("NEXT UTC", "next_due", 20)]
            details = [[(key.replace("_", " ").upper(), self.value(record.get(key))) for key in
                        ("job_id", "name", "state", "when", "next_due")] for record in records]
            empty = "No scheduled jobs"
        else:
            groups = {"platforms": snapshot["platforms"], "harnesses": snapshot["harnesses"],
                      "scheduling": [snapshot["scheduling"]] if snapshot.get("scheduling") else [],
                      "work": [snapshot["work"]]}
            records = [dict(component) for component in groups[self.active_view]]
            identity = {"platforms": "PLATFORM", "harnesses": "HARNESS"}.get(self.active_view, "COMPONENT")
            summary_label = "HEALTH" if self.active_view == "harnesses" else "DETAIL"
            if self.active_view == "harnesses":
                columns = [("STATE", "state", 12), (identity, "id", 14), ("MCP", "mcp", 9),
                           (summary_label, "summary", max(12, self.size.width - 44))]
            else:
                columns = [("STATE", "state", 12), (identity, "id", 18),
                           (summary_label, "summary", max(15, self.size.width - 39))]
            details = []
            for record in records:
                fields = [("STATE", record["state"]), (identity, record["id"]),
                          ("REASON", self.value(record["reason"])), ("SAMPLE AGE", age(record["observed_at"]))]
                if self.active_view == "harnesses":
                    mcp = record["mcp"]
                    fields += [("MCP", self.mcp_label(mcp)), ("MCP REASON", self.value(mcp["reason"])),
                               ("MCP AGE", age(mcp["observed_at"]))]
                fields += [(key.replace("_", " "), self.value(metric["value"]) if metric["value"] is not None
                            else metric["reason"]) for key, metric in record["metrics"].items()]
                details.append(fields)
            empty = "none configured" if self.active_view in {"platforms", "harnesses"} else "unavailable: no provider reports status"
        keys = [key for _, key, _ in columns]
        target = self._sort_target.get(self.active_view, keys[0])
        if target not in keys:
            target = keys[0]
            self._sort_target[self.active_view] = target
        sort = self._sort.get(self.active_view)
        if sort and sort[0] not in keys:
            self._sort.pop(self.active_view)
            sort = None
        if sort:
            key, descending = sort
            state_order = {"running": 0, "ready": 1, "active": 2, "queued": 3,
                           "degraded": 4, "paused": 4, "completed": 5, "ended": 5,
                           "disabled": 6, "unsupported": 6, "unavailable": 6,
                           "disconnected": 6, "cancelled": 7, "failed": 7, "unreported": 7}
            sort_value = (self.component_summary if key == "summary" else
                          (lambda record: (state_order.get(record["state"], 8), record["state"]))
                          if key == "state" else
                          (lambda record: (self.MCP_ORDER.get(self.mcp_label(record["mcp"]), 5),
                                           self.mcp_label(record["mcp"])))
                          if key == "mcp" else lambda record: record.get(key))
            order = [index for index, record in enumerate(records) if sort_value(record) is not None]
            order.sort(key=lambda index: sort_value(records[index]), reverse=descending)
            order += [index for index, record in enumerate(records) if sort_value(record) is None]
            records = [records[index] for index in order]
            details = [details[index] for index in order]
        if self.active_view == "work" and self.work_tree:
            children: dict[str, list[tuple[dict[str, Any], list[tuple[str, str]]]]] = {}
            roots = []
            for record, detail in zip(records, details, strict=True):
                if record["type"] == "subagent":
                    children.setdefault(record["parent"], []).append((record, detail))
                else:
                    roots.append((record, detail))
            ordered = []
            for root, detail in roots:
                ordered.append((root, detail))
                for index, (child, child_detail) in enumerate(children.get(root["id"], [])):
                    child["tree_agent"] = ("└─ " if index == len(children[root["id"]]) - 1 else "├─ ") + self.value(child["agent"])
                    ordered.append((child, child_detail))
            records = [record for record, _ in ordered]
            details = [detail for _, detail in ordered]
        signature = (self.active_view, self.work_tree, columns, records, sort, target)
        if signature == self._table_signature:
            return
        self._table_signature = signature
        note = empty if not records else (
            "harness model list unavailable" if self.active_view == "models" and catalog_unavailable else None
        )
        self._update_table(records, columns, details, sort, target, note)

    def _update_table(self, records: list[dict[str, Any]], columns: list[tuple[str, str, int]],
                      details: list[list[tuple[str, str]]], sort: tuple[str, bool] | None,
                      target: str, note: str | None) -> None:
        """Rebuild cells and retain the selected resource and header hover."""
        keys = [key for _, key, _ in columns]
        self._details = details
        table = self.query_one(DataTable)
        hover = table.hover_coordinate if columns == self._columns and table.hover_coordinate.row == -1 else None
        self._columns = columns
        selected = table.cursor_row
        selected_id = self._row_ids[selected] if selected < len(self._row_ids) else None
        table.clear(columns=True)
        for label, key, width in columns:
            if sort and key == sort[0]:
                label = f"{label[:width - 2]} {'↓' if sort[1] else '↑'}"
            table.add_column(label, width=width)
        for record in records:
            cells = []
            completed = (self.active_view == "work" and record.get("type") == "subagent"
                         and record["state"] == "completed")
            dim_row = ((self.active_view == "work" and (record["state"] == "queued" or completed)) or
                       (self.active_view == "scheduling" and record["state"] in {"paused", "ended"}))
            for _, key, width in columns:
                if key == "summary":
                    value = self.component_summary(record)
                elif key == "mcp":
                    value = self.mcp_label(record["mcp"])
                elif key == "when":
                    value = self.schedule_summary(record[key])
                elif key == "started_at":
                    value = self.work_started(record.get(key))
                elif key == "elapsed_seconds":
                    value = self.format_elapsed(record[key])
                elif key == "selected":
                    value = "yes" if record[key] is True else "no" if record[key] is False else "—"
                elif key == "agent" and self.active_view == "work" and self.work_tree:
                    value = record.get("tree_agent") or self.value(record.get(key))
                else:
                    value = self.value(record.get(key))
                style = (self.state_color(value) if key == "state" else
                         self.mcp_color(value) if key == "mcp" else "")
                if completed and self.console.color_system == "truecolor":
                    style = "#8b9298" if self.selected_theme == "dark" else "#636b72"
                text = Text(value, style=f"{style} dim".strip() if dim_row else style)
                text.truncate(width, overflow="ellipsis")
                cells.append(text)
            table.add_row(*cells)
        self._row_ids = [f"{record['owner']}:{record['role']}:{record['model_name'] if record['role'] == 'harness' else record['id']}"
                         if self.active_view == "models" else
                         str(record.get("id") or record.get("job_id") or index)
                         for index, record in enumerate(records)]
        if records:
            row = self._row_ids.index(selected_id) if selected_id in self._row_ids else min(selected, len(records) - 1)
            table.move_cursor(row=row)
        if hover is not None:
            table.hover_coordinate = hover
        column = (keys.index(target) + 1, len(keys)) if records else None
        self.query_one("#keys", Static).update(self.key_hints(column=column, empty=note))

    def key_hints(self, *, column: tuple[int, int] | None = None, empty: str | None = None) -> Text:
        accent = self.get_css_variables()["accent"]
        hints = ([("1-5", "tabs"), ("q", "quit")] if column is None else
                 [("1-5", "tabs"), ("↑↓", "rows"), ("Enter", "details"),
                  ("c", f"col {column[0]}/{column[1]}"), ("s", "sort"), ("q", "quit")])
        if self.active_view == "work":
            if self.size.width < 64:
                # Omit Enter: peek already fills the 58-cell chrome budget.
                hints = [("1-5", "tabs"), ("t", "list" if self.work_tree else "tree"),
                         ("p", "peek"), ("x", "stop"), ("d", "drop"), ("q", "quit")]
            elif self.size.width < 100:
                hints = [("1-5", "tabs"), ("↑↓", "rows"), ("Enter", "details"),
                         ("c/s", "sort"), ("t", "tree"), ("p", "peek"), ("x", "stop"), ("d", "drop"), ("q", "quit")]
            else:
                hints[-1:-1] = [("t", "list" if self.work_tree else "tree"), ("p", "peek"),
                                ("x", "stop"), ("d", "drop")]
        if self.active_view == "scheduling" and column is not None:
            if self.size.width < 64:
                hints = [("1-5", "tabs"), ("Enter", "info"), ("Space", "pause"),
                         ("Del", "delete"), ("q", "quit")]
            elif self.size.width < 100:
                hints = [("1-5", "tabs"), ("Enter", "details"), ("Space", "pause/resume"),
                         ("Del", "delete"), ("q", "quit")]
            else:
                hints[-1:-1] = [("Space", "pause/resume"), ("Del", "delete")]
        text = Text()
        for key, action in hints:
            if text:
                text.append("  ")
            text.append(key, style=f"bold {accent}")
            text.append(f" {action}")
        if empty:
            text.append(f"  ·  {empty}", style="dim")
        return text

    def status_header(self, running: str, queued: str, state: str, updated: str) -> Text:
        """Keep Belle left and status, load, and local time right."""
        symbol, severity = {"connected": ("●", "ready"), "stale": ("◐", "degraded"),
                            "connecting": ("◌", "degraded"), "disconnected": ("○", "error")}[state]
        right = f"{symbol} {state.upper()} | running {running}  queued {queued} | {updated}"
        compact = cell_len(right) + 6 > self.size.width - 2
        if compact:
            right = f"{symbol} {state.upper()} | r {running}  q {queued} | {updated}"
        free = self.size.width - 2 - cell_len("Belle") - cell_len(right)
        # Keep decoration out of a 60-column split even if a few cells remain.
        ring_slots = min(3, max(0, (free - 3) // 2)) if self.size.width >= 72 else 0
        accent = self.get_css_variables()["accent"]
        brand = Text("Belle", style=f"bold {accent}")
        left_rings = Text(" " * (ring_slots + 1)) if ring_slots else Text()
        right_rings = Text(" " * (ring_slots + 2)) if ring_slots else Text(" ")
        if self._brand_burst_frame >= 0:
            phase = self._brand_burst_frame % 9
            # Each pulse travels outward; the inner rings fade first.
            if phase <= 2:
                peak = "#fff2be" if self.selected_theme == "dark" else "#cf8c25"
                style = (f"bold {peak}" if self.console.color_system not in {"standard", "windows"}
                         else f"bold reverse {accent}")
                for index in range(5):
                    distance = abs(index - 2)
                    if distance == phase:
                        brand.stylize(style, index, index + 1)
                    elif distance == phase - 1:
                        brand.stylize(f"bold {accent}", index, index + 1)
            else:
                depth = min(phase - 2 if phase <= 5 else 8 - phase, ring_slots)
                ring_indices = range(depth) if phase <= 5 else range(ring_slots - depth, ring_slots)
                left_chars = [" "] * (ring_slots + 1)
                right_chars = [" "] * (ring_slots + 2)
                for index in ring_indices:
                    left_chars[ring_slots - index - 1] = "("
                    right_chars[index + 1] = ")"
                left_rings = Text("".join(left_chars)) if ring_slots else Text()
                right_rings = Text("".join(right_chars)) if ring_slots else Text(" ")
                for index in ring_indices:
                    # The ring nearest Belle stays stronger than the outer rings.
                    if index == 0:
                        style = f"bold {accent}"
                    elif self.console.color_system in {"standard", "windows"}:
                        style = f"dim {accent}"
                    else:
                        softer_accent = "#6c460d" if self.selected_theme == "light" else "#80602c"
                        style = softer_accent
                    left_rings.stylize(style, ring_slots - index - 1, ring_slots - index)
                    right_rings.stylize(style, index + 1, index + 2)
        gap = max(0, free - (2 * ring_slots + 3 if ring_slots else 1))
        header = Text.assemble(left_rings, brand, right_rings, " " * gap)
        header.append(f"{symbol} {state.upper()}", style=self.state_color(severity))
        header.append(" | ")
        header.append("r" if compact else "running", style="dim bold")
        header.append(f" {running}  ")
        header.append("q" if compact else "queued", style="dim bold")
        header.append(f" {queued} | {updated}")
        return header

    @staticmethod
    def schedule_summary(schedule: str) -> str:
        """Compact cron fields in the row; details keep the complete schedule."""
        if not schedule.startswith("cron "):
            return schedule
        fields = dict(re.findall(r"([a-z_]+)=(.*?)(?= [a-z_]+=|$)", schedule))
        expression = fields.get("expr")
        zone = fields.get("zone")
        if expression is None or zone is None:
            return schedule
        second = fields.get("second", "0")
        suffix = f" +{second}s" if second != "0" else ""
        return f"{expression} ({zone}){suffix}"

    @staticmethod
    def format_elapsed(seconds: int | None) -> str:
        if seconds is None:
            return "—"
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"

    @staticmethod
    def value(value: Any) -> str:
        """Keep unknown values distinct from zero."""
        return "—" if value is None else str(value)

    def component_summary(self, component: Any) -> str:
        """Preserve explicit measurement reasons and old sample ages."""
        values = [f"{key.replace('_', ' ')}={metric['value'] if metric['value'] is not None else metric['reason']}"
                  for key, metric in component["metrics"].items()]
        if component["reason"]:
            values.insert(0, component["reason"])
        sample_age = age(component.get("observed_at"))
        if sample_age != "unknown" and int(sample_age[:-1]) > self.poll_seconds * 2:
            values.append(f"age: {sample_age}")
        return "  ".join(values).replace("_", " ") or "—"

    MCP_ORDER: ClassVar[dict[str, int]] = {"ready": 0, "unknown": 1, "missing": 2, "failed": 3, "n/a": 4}

    @staticmethod
    def mcp_label(mcp: Mapping[str, Any]) -> str:
        """Show not_applicable as n/a; other states stay as reported."""
        state = mcp["state"]
        return "n/a" if state == "not_applicable" else state

    def mcp_color(self, label: str) -> str:
        """Color MCP labels the same way as harness state, without relying on color alone."""
        if label == "n/a":
            return ""
        mapped = {"ready": "ready", "unknown": "degraded"}.get(label, "failed")
        return self.state_color(mapped)

    def state_color(self, state: str) -> str:
        """Use color only to reinforce the written state."""
        if state == "completed":
            if self.console.color_system in {"standard", "windows"}:
                return "white"
            return "#8b9298" if self.selected_theme == "dark" else "#636b72"
        if self.console.color_system in {"standard", "windows"}:
            if state in {"ready", "running", "active"}:
                return "green"
            if state in {"queued", "degraded", "paused", "cancelled", "unreported"}:
                return "yellow"
            return "red"
        if state in {"ready", "running", "active"}:
            return "#73c991" if self.selected_theme == "dark" else "#216439"
        if state in {"queued", "degraded", "paused", "cancelled", "unreported"}:
            return "#ecc679" if self.selected_theme == "dark" else "#7a5417"
        return "#e79b98" if self.selected_theme == "dark" else "#a12d28"

    def work_started(self, timestamp: str | None) -> str:
        """Include the date for executions that started before today."""
        if timestamp is None:
            return "—"
        started = datetime.fromisoformat(timestamp)
        today = datetime.fromisoformat(self.snapshot["generated_at"]).date() if self.snapshot else datetime.now(UTC).date()
        return started.strftime("%H:%M:%SZ" if started.date() == today else "%m-%d %H:%M:%S")
