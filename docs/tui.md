# Operator terminal

Install the optional client from this checkout:

```sh
python -m pip install '.[tui]'
belle-tui --socket "$BELLE_AGENT_RUNTIME_DIR/control.sock"
```

Set `control.enabled: true` in Belle's YAML before starting it. The client
runs as a separate process. It reads status through the socket. In Jobs,
Space pauses or resumes the selected job and Delete removes it. In Activity,
`x` stops a running main turn and `d` drops a queued main message. Each destructive
action names its target in a confirmation dialog over the live view. Left and
Right select a button; Enter activates it, and Escape cancels. Closing the
client leaves Belle running.

Without `--socket`, the client uses `BELLE_AGENT_RUNTIME_DIR/control.sock`.
If that variable is unset, it uses `$XDG_RUNTIME_DIR/belle-agent/control.sock`.
It reports a configuration error when neither runtime directory is available.
Use `--theme light` for a light terminal palette. The default is
`--theme dark`. Health states have matching words, symbols, and colors.

## Read the screen

The resource browser gives the selected table the full available height. The tabs
are Activity, Jobs, Platforms, Harnesses, and Models; Activity opens by
default. Press `[1]`–`[5]` to switch views, arrows to select a row, and `Enter` to
read its complete values.

On a running main or subagent Activity row, `p` opens the live peek pane; the
server accepts peeks only when `control.peek` is true. A main peek shows all
frames in the ring; a subagent peek shows that child's frames (by
`source_work_id`) plus the turn outcome. A main peek also shows OpenCode
child-session frames, tagged with the child's `source_work_id`. The pane
follows new output and scrolls back over the frames Belle still holds,
including a one-time `peek_history` seed when the backend advertises it. Frame
kinds and limits are in the [status protocol](control/protocol.md#live-peek).
OpenCode reconnect gaps are in the
[OpenCode guide](backends/opencode.md#lost-connections).

Answer text and thinking are indented two cells. Meta lines stay flush-left
(`→` / `←` tools, `?` question, `#` usage; a labelled `· ── name ──` step only
when the harness sends a step label). Completed markdown blocks render as
markdown; the open edge stays plain; thinking is dim italic. When the harness
reports turn usage, a dim `# tokens …` line appears. A finished turn ends with
a dim end line (`Worked for … - HH:MM` plus optional reason); the title suffix
shows succeeded, failed, or interrupted (for example `· failed`). Evicted
frames show as `· ── N frames omitted ──`. Scrollback is the in-memory ring
only (`control.peek_max_frames` / `peek_max_bytes`).

Tool rows stay collapsed as one-line meta. When a bounded `args_text` or
`result_text` is present, the row shows `+`; press `+`, `=`, or Enter (or
click) to expand, `-` to collapse. An expanded row shows the body below the
tool line, after an accent-colored `└`.

`Esc` closes the peek; the turn keeps running.

Press `Esc` or `Enter` to close details, `r` to refresh, or `q` to exit. The
footer shows the controls or an empty-state reason.
Click a column header to cycle ascending, descending, and source order.
For keyboard sorting, press `c` to choose a column and `s` to cycle its order.
An arrow in the header marks the active sort. Sorting stays in place when data refreshes.
The status tabs start sorted by State; Models starts sorted by role.

The full-width status line keeps Belle on the left. Connection health, running
and queued-message counts, and local update time are aligned right, in that order.
The Belle name briefly rings outward every ten seconds when color and motion
are enabled. Narrow terminals omit the rings to keep status in view. Set
`TEXTUAL_ANIMATIONS=none` to keep the header still; `NO_COLOR` also disables
the effect.
A dash means the count is unknown. The client polls every two seconds.
A failed read retains the last snapshot and labels it `stale`; a snapshot older
than four seconds is also stale. Reconnecting replaces it after a Belle restart.
Before the first successful read, the line shows `connecting` or `disconnected`.

Scheduling shows the Belle scheduler's actual jobs: state, name, recurrence,
and (at 80 columns) next due UTC time. Cron rows use `expression (timezone)`;
Enter shows the complete job ID and
schedule. Paused and ended jobs remain visible in dim text. No jobs means an empty
schedule, while an unavailable provider remains explicitly unavailable. The
provider reads a detached cache from existing scheduler transactions; opening
or refreshing the TUI never opens the job store or changes scheduler state.

In Jobs, press `Space` on an active or paused row to toggle its state. Press
`Delete` on any job row to remove it after confirmation. A Belle-themed
notification reports success or failure. Ended jobs need a new schedule and
cannot be toggled. Running jobs cannot be deleted; stop the current run in
Activity first. Deleting an idle, paused, or ended job cancels pending
occurrences and discards the job and its history. Pausing leaves running turns
alone. Resuming wakes the scheduler and uses the job's existing catch-up policy.
The server checks the selected job ID, displayed state, and server instance
before writing under the scheduler's store lock. Refresh and retry if the job
changed or Belle restarted.
A disconnected or stale view refuses the action. A connection timeout may
occur after the change committed; refresh Jobs to inspect its current state.
Other tabs and the details screen remain read-only.

Platforms show state, the platform name, and DETAIL (counts or a reason).
Harnesses show state, the harness name, MCP, and HEALTH. MCP is `ready`,
`missing`, `failed`, `unknown`, or `n/a` when the harness has no remote health
probe or does not use Belle tools through MCP. HEALTH is the cached sample
label or reason, for example `event stream reconnecting` or `unreachable`. A
harness sample older than four seconds becomes `unavailable` with
`health_sample_stale`, and MCP becomes `unknown` with the same reason. Enter
shows raw reason codes, MCP reason and age, measurements, and sample age. A
measured `0` differs from `unsupported` or `unavailable`. Empty lists show
`none configured`. Other component samples older than four seconds show their
age.

Models lists Belle's decision, summarizer, extractor, and classifier entries
alongside models reported by the active harness. The Owner column says `Belle`
for Belle task models and names the harness for its models. Harness entries
show the `harness` role and use `—` for API and selected status because the
backend catalog supplies only model names. The footer reports when the harness
model list is unavailable.
Credentials and endpoint URLs are absent from the TUI status data.

Activity lists running turns, queued messages, and backend-reported subagents.
Each subagent has its own row. Select a running main turn and press `x` to stop
it; select a queued main message and press `d` to drop it. Both require
confirmation. Subagent rows remain read-only. Press `t` to switch between a
sortable flat list with a `PARENT` column and a tree grouped beneath each parent
turn. Completed subagents remain visible, dimmed, until the parent ends. Queued
messages are also dim. The table shows state, type, agent, platform, and conversation at
60 columns. Wider terminals add local ID and harness at 80 columns. They add
parent and `TIME+` at 100, model at 120, tokens at 140, and UTC start at 160.
Tree view uses the agent column for branches and omits the parent column.
`TIME+` measures elapsed wall time. Queued messages show `—`. Columns
truncate without wrapping. `Enter` reveals all fields at every width.
Unknown values show `—`. Zero tokens show `0`. Platform, workspace, and
conversation values are provider-reported identities, never guessed names.

At 80×24 and in a 60-column split, the table retains at least sixteen data and
header rows; large datasets scroll within the table. Polling preserves selection
and scroll. Both themes use written state labels with sparse health colors.
The screen shows no message bodies, credentials, prompts, or work history.
See [the status protocol](control/protocol.md) for the exact measurements.

## Run in a separate rootless container

A client container needs the socket directory and a matching UID. Mount the
directory so a replacement socket remains visible after Belle restarts.
Do not mount Belle's data, state, or configuration directories.

Build a client image from this checkout:

```sh
podman build -f containers/tui/Containerfile -t localhost/belle:tui-local .
```

Run it with the same directory that Belle uses for its status socket:

```sh
podman run --rm -it --userns=keep-id --user "$(id -u):$(id -g)" \
  --network none -e TERM -e COLORTERM \
  -v /etc/localtime:/etc/localtime:ro \
  -v "$BELLE_AGENT_RUNTIME_DIR:/run/belle:ro,z" \
  localhost/belle:tui-local --socket /run/belle/control.sock
```

Pass through your terminal settings for Belle's exact colors. With 256 colors,
the client uses the nearest palette colors. Podman's default `TERM=xterm`
selects basic ANSI colors; health labels and symbols retain their meaning.
The `/etc/localtime` mount makes the displayed update time match the host.

For a separate Belle container, mount that same host directory read-write at
Belle's configured runtime directory. Both processes must have permission to
traverse the directory and connect to the socket. With SELinux, use a shared
container label (`:z`) on both mounts. A read-only mount permits connecting to
the Unix socket while preventing filesystem changes from the client container.
The image installs Belle's mandatory dependencies as well as Textual.
