# Local status protocol

Set `control.enabled: true` in Belle's YAML configuration. Start Belle with
`BELLE_AGENT_RUNTIME_DIR` set to an absolute directory owned by its UID with
mode `0700`. Alternatively, set `XDG_RUNTIME_DIR` and use its `belle-agent`
subdirectory. Belle creates a missing runtime directory with mode `0700`.
An inaccessible or shared directory disables status serving with a warning.

The standalone Belle host starts the status service after its platforms start.
It stops the service before it stops the platforms. Other host integrations
must register their own providers and lifecycle. Status serving is disabled by
default. It requires no TUI dependency.

The socket is `control.sock` with mode `0600`. Use the same UID for a reader.
Mount the runtime directory when a reader runs in another container. The
socket file changes after a restart. Mounting the directory lets the reader
find the new socket. Do not mount Belle's config or state for status reading.

Read one snapshot:

```sh
python -m belle_agent.control.client --socket "$BELLE_AGENT_RUNTIME_DIR/control.sock"
```

The Python interface is:

```python
from pathlib import Path
from belle_agent.control.client import get_snapshot

snapshot = await get_snapshot(Path("/run/belle-agent/control.sock"))
```

`get_snapshot(socket, *, request_id=None, timeout=2.0)` returns a validated
`Snapshot` dictionary. A protocol or server refusal raises
`belle_agent.control.protocol.ControlError` with `code` and `message`.
Transport failures raise `OSError` or `TimeoutError`.

## JSON lines

Each connection accepts one UTF-8 JSON object followed by a newline. Belle
returns one object and closes the connection. Each valid request has a
nonempty string ID. Its response repeats that ID. Malformed requests with no
usable ID receive `id: null`. Version 4 requests use `"version": 4`. Other integers return `unsupported_version`. Version 4 accepts exactly these request fields:

```json
{"version":4,"id":"probe-1","method":"snapshot.get"}
```

Success has `version`, `id`, and `result`. Failure has `version`, `id`, and
`error`, where error has `code` and `message`. `snapshot.get` has no request parameters. The write methods are
`schedule.toggle`, `schedule.delete`, `work.stop`, and `work.drop`:

```json
{"version":4,"id":"toggle-1","method":"schedule.toggle","params":{"job_id":"job-1234567890","expected_state":"active","instance_id":"instance-from-snapshot"}}
```

`params` must contain exactly three fields: the target ID, `expected_state`,
and `instance_id`. Each ID is a nonempty string. The server checks its
instance ID first. Success returns the same snapshot shape as `snapshot.get`,
with the updated state.

| Method | Target | `expected_state` | Effect |
| --- | --- | --- | --- |
| `schedule.toggle` | `job_id` | `active` or `paused` | Pauses an active job or resumes a paused one. |
| `schedule.delete` | `job_id` | `active`, `paused`, or `ended` | Deletes the job and its history, and stops its running runs. |
| `work.stop` | `work_id` | `running` | Stops one running main turn. |
| `work.drop` | `work_id` | `queued` | Drops one queued main message or one pending scheduled run. It never stops a running run. |

The schedule methods check the job's current state under the store lock. They
refuse missing jobs and changed states. `schedule.toggle` also refuses an
ended job. A resumed job's next due time appears after the scheduler
replans. The work methods refuse work that is gone or has changed state.
`work.stop` returns `unsupported` when the harness cannot cancel a turn.
Hosts without an action handler or scheduler return `unsupported`. Other
methods return `unknown_method`.

Socket access authorizes these local operator actions across scheduled jobs and work.
The private directory and same-UID socket permissions protect that privilege;
a read-only directory mount still allows socket write requests. Pausing does
not stop running turns. Resuming follows the existing catch-up policy.
Timeouts and transport failures can happen after a write commits. Read a fresh
snapshot before retrying; repeating the same expected state refuses a changed
job rather than reversing the completed write.

| Code | Meaning |
| --- | --- |
| `bad_json` | The line is invalid UTF-8 or JSON. |
| `invalid_request` | A field, shape, measurement, or duplicate key is invalid. |
| `unsupported_version` | The version is not integer 4. |
| `unknown_method` | The method is not a known status, write, or peek method. |
| `unsupported` | The host has no scheduler action handler or Belle scheduler. |
| `stale_selection` | Belle restarted since the selected snapshot, or the selected job or work changed state or no longer exists. |
| `unavailable` | The host is not running, so it cannot stop or drop work. |
| `job_not_found` | The selected scheduled job no longer exists. |
| `job_ended` | The job needs a new schedule before resuming. |
| `no_change` | The job state differs from `expected_state`. |
| `action_failed` | The scheduler action failed; refresh before retrying. |
| `message_too_large` | The encoded request or response exceeds its byte budget. |
| `peek_disabled` | `control.peek` is false. |
| `peek_unavailable` | The work id is not a running main or subagent row. |
| `peek_ended` | The peek closed, drained, or its buffer was discarded; idle subscribers are reclaimed on the next open/poll that reaps. |
| `peek_subscriber_limit` | Too many concurrent peeks. |

The default server budgets are 4,096 request bytes, 262,144 response bytes,
eight simultaneous clients, 2 seconds per request and socket close, and 0.25 seconds per
provider. These bounds protect the host's descriptors, memory, and event loop.
The constructor's `ControlSettings` arguments select different service budgets
for embedded deployments. YAML exposes `control.enabled` (default false),
`control.peek` (default false) and the peek resource limits
`peek_max_frames` (200), `peek_max_bytes` (65,536),
`peek_max_subscribers` (4), and `peek_subscriber_idle_seconds` (300). See
[Local status socket](../config.md#local-status-socket).
A client whose response exceeds 262,144 bytes refuses it explicitly.
Idle or timed-out connections close. A connection above the client budget
closes before Belle reads it. A response that exceeds the response budget
returns its measured size and the `max_response_bytes` argument to change,
when the error itself fits the budget. No result is truncated.

## Snapshot fields

`result` has `instance_id`, `generated_at`, `platforms`, `harnesses`,
`work`, `scheduling`, and `task_models`, plus optional `build` and
`harness_models`. The `build` object contains
`version` and `revision`, each a nonempty string or null. These identify the
installed server distribution; revision comes from installed Git direct-URL
metadata. A server without this metadata reports null. Older snapshots can
omit `build`. The first two are a service UUID and UTC ISO 8601
timestamp. Each successful service start creates a new UUID.
`platforms` and `harnesses` are arrays. `work` and `scheduling` are components.
Each component has these fields. Harnesses also require `mcp`; other components
must omit it.

| Field | Type and meaning |
| --- | --- |
| `id` | Technical component ID. It contains no workspace or person name. |
| `state` | `ready`, `disabled`, `unsupported`, `unavailable`, `disconnected`, or `degraded`. |
| `reason` | Nonempty for `unsupported` or `unavailable`. For `degraded` or `disconnected`, a short label or null. Null for `ready` or `disabled`. |
| `observed_at` | UTC ISO 8601 sample time, or null when no sample exists. |
| `metrics` | Named measurements. Each has `value` and `reason`. |
| `mcp` | Harness components only, and required on every one: whether the harness can use Belle's MCP tools. An object with exactly `state`, `reason`, and `observed_at`. |

`mcp.state` is `ready`, `missing`, `failed`, `unknown`, or `not_applicable`.
`mcp.reason` is null for `ready` and `not_applicable`, and a nonempty safe code
without control characters otherwise, such as `mcp_mount_missing`. Host and
collector paths also use `unreachable`, `health_sample_pending`,
`health_sample_stale`, `health_sample_invalid`, `provider_timeout`, and
`provider_failed`. `mcp.observed_at` is a UTC ISO 8601 time or null. A harness
without `OP_HEALTH` reports `not_applicable`. A failed or invalid probe, or a
stale or pending sample, reports `unknown` with that cause as the reason.
Platforms, `work`, and `scheduling` carry no `mcp` field.

A measurement value is a nonnegative integer or null. A measured value has
`reason: null`. A null value requires a nonempty reason. Measured zero and an
unsupported count remain distinct. Provider failure affects only that
provider's component. The reasons are `provider_timeout` and `provider_failed`.
Provider exception text never enters a response.

Snapshot time records local collection. `OP_HEALTH` harnesses are sampled in
the background under a host probe deadline; `snapshot.get` reads the cache
only. A probe that times out or raises stores state `disconnected` with
reason `unreachable`. Treat the previous snapshot as stale after a failed
refresh. Preserve its original timestamp. A changed `instance_id` identifies
a new service instance.

## Version 4 field inventory

The current host has one backend and multiple named platform adapters. An
adapter can itself serve multiple workspaces. Version 4 uses one row per
adapter because workspace connection health has no common provider contract.
Work rows contain active executions and queued messages. Queued events remain
a count. Work rows include originating platform, workspace and conversation identifiers.
Scheduling rows include local job identifiers and operator-supplied job names.
Task model rows list configured Belle model entries, including their role,
entry ID, API, model name, and selected entry. `harness_models` is null when
the backend does not advertise a model catalogue or the catalogue is unavailable
within the provider timeout. Otherwise it lists the backend's model
names and owner. The host caches results for 30 seconds. A failed refresh
reports null and does not retain older rows. No API keys or endpoint URLs enter
the snapshot.

| Field | Owner and read path | Accuracy and freshness | Privacy and restart |
| --- | --- | --- | --- |
| `work.metrics.queued_messages` | `ConversationHost._admission.queued_messages`, sum of list lengths | Exact at `observed_at`. Admission and draining change it on the host event loop. No cached sample. | Count only. Resets with host memory. |
| `work.metrics.queued_events` | `AdmissionState.pending_count()` | Exact count of distinct pending and admitting events at `observed_at`. Includes in-flight admission. | Count only. Resets with host memory. |
| `work.metrics.active_turns` | Host-owned live execution registry | Exact at `observed_at`; registered on start and removed after delivery/cleanup, independently of admission lookup expiry. | Count only. Resets with host memory. |
| `platforms[].state` | Registered platform names | `unsupported` with `remote_health_not_measured`, null `observed_at` | Technical names only. Rebuilt on start. |
| `harnesses[].state` | Backend capability name | Background `OP_HEALTH` cache (`ready` / `degraded` / `disconnected` with label, or `unavailable` when pending, stale, or invalid). Without `OP_HEALTH`: `unsupported` / `remote_health_not_measured`. | Technical names only. Rebuilt on start. |
| `harnesses[].mcp` | Same `OP_HEALTH` sample as `harnesses[].state` | Same cache and stale window. `observed_at` matches the harness sample when a probe ran; null for pending, stale, invalid, or no-`OP_HEALTH` (`not_applicable`). | Reason codes only; never mount `error` text. |
| `scheduling` | Belle Runner cache, updated during existing store transactions | State, sample timestamp, enabled/total jobs and running/pending run counts; unavailable when the store cannot be read | Detached safe job summaries; status reads never open the state file. |
| `task_models` | Belle model configuration, captured when the host starts | Exact configured entries at host start; `selected` marks the role's configured choice, including a Second Brain summarizer selection | Names and API types only; no credentials or endpoint URLs. |
| `harness_models` | Active backend catalog, refreshed at most every 30 seconds when it advertises model listing | Names returned by the backend catalog; null when unsupported, unavailable, or timed out | Backend name and model names only. |

`work.state` reports whether the local host admits work. Its sample does not
query a platform or backend. Each provider returns a detached value before
socket writing starts. No admission lock is held during client I/O.

Providers must return only technical IDs, health labels, timestamps, counts,
originating workspace and conversation IDs, and the documented scheduler job
names and recurrence fields. They must exclude message bodies, prompts,
credentials, people names,
and file paths. Protocol fixtures use synthetic values. The collector accepts
no arbitrary host objects or exception messages.

## Current work rows

The `work` component may include `rows`. An absent array means the provider
cannot report individual work items; an empty array means no active turns or
queued messages. Each row has exactly `id`, `state`, `type`, `agent`, `parent`,
`harness`, `started_at`, `platform`, `workspace`, `conversation`, `model`,
`tokens_in`, and `tokens_out`. IDs are opaque local UUIDs, stable when a queued
message starts, and independent of platform or conversation IDs.

`type: main` rows have `running` or `queued` state and null `agent` and
`parent`. Running includes host preparation, backend execution, delivery and
cleanup. `type: subagent` rows carry the backend-reported agent name, when
available, and their main row's ID in `parent`. Their state is `running`,
`completed`, `failed`, `cancelled`, or `unreported`. Completed subagents remain
visible until their parent turn ends. Backends that do not report subagent
events produce no subagent rows. `harness` is the technical backend name.
`platform` is the originating technical platform name. `workspace` and
`conversation` identify the originating workspace and conversation; a
conversation ID can be reused across workspaces. Each field is null when
unavailable. Scheduled and event-triggered
executions use their inbound origin too. Workspace and conversation IDs can be sensitive;
they are exposed only to readers with access to the same-UID local socket.
`started_at` is a UTC ISO 8601 timestamp for running work and null for queued
messages. `model` is the turn request's settled scope model when set, else
null. `tokens_in` / `tokens_out` stay null until the backend emits
turn-cumulative `usage` for that turn; session totals are not substituted.
Non-null token counts must be nonnegative integers.

Rows copy the host's exact live registry and its owned waiting-message queues
on the host event loop. Steered messages are part of their current execution;
queued events are not message rows. Polling can miss work completed between
samples. Rows are never truncated: the existing response byte budget refuses
an oversized snapshot. Rows include no message text, user names, prompts,
credentials, file paths, or raw backend objects.

## Scheduling rows

A Belle scheduler provider may include `rows`; absent means individual jobs are
not available, while `[]` means no jobs. Each row has exactly `job_id`, `name`,
`state`, `when`, and `next_due`. IDs are unique within the array. `state` is
`active`, `paused`, or `ended`. `when` is a complete textual representation of
the stored recurrence fields, including timezone and stop constraints.
`next_due` is a UTC ISO 8601 timestamp or null when no future run is planned.
The provider excludes prompts, delivery addresses, origin, users, backend turn
settings and run results. Job names and schedule details are visible to the
same-UID operator who can read the status socket. A provider reads only the
Runner's detached cache; it performs no store I/O, claims or dispatches.

## Live peek

When `control.peek` is true, the socket also accepts content-bearing peek
methods for a running main or subagent Activity row. `snapshot.get` stays
content-free.

| Method | Params | Result |
| --- | --- | --- |
| `peek.open` | `work_id`, `instance_id` | `peek_id`, `work_id`, `live`, `lost`, `frames` |
| `peek.poll` | `peek_id`, `after_seq` | same shape; frames with `seq` greater than `after_seq` |
| `peek.close` | `peek_id` | `{closed: true}` |

`work_id` may be a running main row or a running subagent row. The ring is
always stored under the main turn. Opening a subagent returns only frames
whose `source_work_id` is that child, plus terminal `outcome` frames.
`peek.open`'s result `work_id` is the Activity row that was opened.

Each frame has `seq`, `at`, `work_id`, `source_work_id`, `kind`, and `payload`.
`work_id` is the main turn Activity id; `source_work_id` is that same id for
parent-attributed frames, or the subagent Activity id when the event belongs
to a tracked child.
Kinds and payloads:

| Kind | Payload |
| --- | --- |
| `text` / `thinking` | `{text}` |
| `step` | `{label}`: host tees a step only when `label` is a non-empty string; bare iteration steps are omitted |
| `tool_call` | `{call_id, name, detail, args_text}`: `detail` is an optional short label from `arguments.description`; `args_text` is optional bounded JSON of scrubbed arguments (null when absent, oversize, or unserializable) |
| `tool_result` | `{call_id, name, ok, summary, result_text}`: `name` / `summary` / `result_text` may be null; `result_text` is an optional bounded result body |
| `question` | `{prompt_summary, kind}`: single-line sanitized prompt; no secrets |
| `usage` | `{tokens_in, tokens_out, cost}`: each field is optional; null when the backend did not report it; counts are turn-cumulative when the harness scopes them |
| `outcome` | `{state, duration_seconds, ended_at, extra}` |

`outcome.state` is `succeeded`, `failed`, or `interrupted`. `duration_seconds` may
be null. `ended_at` is a UTC ISO 8601 timestamp when the host recorded the
outcome. `extra` is optional end-line operator metadata (for example a failure
or interrupt reason); it may be omitted when too large (the host retries the
outcome frame without `extra`, similar to tool bodies). Assistant answer text
is already in `text` frames.

### Tool frame bodies

`args_text` and `result_text` are always present and null when the frame has no body.
Bodies allow tab and newline; other control characters are refused.
Secret-shaped keys are redacted. Absolute filesystem paths are reduced to
basenames (path-named keys use basename-only values; a plain result that is
itself an absolute path is reduced the same way). Raw absolute paths are
not teed. Each body is bounded (`MAX_PEEK_TOOL_BODY_BYTES` in the host
helpers); an oversize body is omitted whole. A non-text frame that still
exceeds `control.peek_max_bytes` is refused with `message_too_large` (setting
name and size); the host retries the tool frame without the body. A peek tee
error leaves the turn running. Oversized `text` / `thinking` payloads are
split into multiple frames that each fit the budget.

### Buffer

Buffers are created on open (or reserved just before `peek_history` catch-up),
filled from the host turn stream, and discarded as soon as the last subscriber
closes. `peek.close` unsubscribes; the turn keeps running. Belle does not keep
a turn transcript of its own. When the buffer is new and the backend advertises
`peek_history`, the host seeds the ring once with prior harness text for that
request (including attributed child-session text when the harness routes it).
Text teed while history is awaited can also appear in that seed, so the same
text can appear twice. Otherwise the peek starts at the live stream. Ring
eviction under `peek_max_frames` / `peek_max_bytes` drops oldest frames when
the buffer is full. A subscriber with no open or poll for
`peek_subscriber_idle_seconds` is closed on the next open or poll that reaps
(not instantly) so crashed clients do not hold slots.
`lost` counts frames that matched this subscriber's filter, were ring-evicted,
and had a `seq` greater than any frame already returned to that subscriber
(reset on each successful open or poll). Clients should show an omission
marker when `lost` is positive. OpenCode reconnect gaps:
[OpenCode backend guide](../backends/opencode.md#lost-connections).
