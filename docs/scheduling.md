# Scheduled jobs

Belle can start a model turn later or on a recurring schedule. A job stores its prompt, time, creator, and delivery address. Each run has its own record. The reply goes to the conversation that the job selects.

## Select the scheduler

`scheduling.engine` defaults to `auto`.

| Backend | `auto` | `belle` | `host` |
| --- | --- | --- | --- |
| OpenCode, Docker Agent | Belle's scheduler | Belle's scheduler | Refused: no host scheduler |
| JiuwenSwarm | JiuwenSwarm's scheduler | Refused | JiuwenSwarm's scheduler |

`disabled` serves no Belle schedule tools or commands. It does not disable a scheduler owned by the backend. Detached sessions and `/belle bg` need Belle's scheduler, so JiuwenSwarm has neither. JiuwenSwarm's scheduler runs each job in a new session, so it has no `session: shared`.

On JiuwenSwarm, the agent keeps its `cron_*` tools. On OpenCode and Docker Agent, Belle serves six `schedule_*` tools. A turn never gets both sets. See [configuration](config.md#scheduling).

## Create and manage jobs

Ask Belle to schedule a task in the conversation where the result belongs. The `schedule_create` tool needs a name, a prompt, and a `when` object. For example, a weekday reminder uses:

```json
{
  "name": "Weekday review reminder",
  "prompt": "Remind this conversation to review open pull requests.",
  "when": {"kind": "cron", "expr": "0 9 * * 1-5", "zone": "Europe/Paris"}
}
```

| Tool | Purpose |
| --- | --- |
| `schedule_create` | Create a job and show its next three runs. |
| `schedule_list` | List visible jobs, or inspect one job. |
| `schedule_update` | Change or pause a job (`enabled: false`). |
| `schedule_delete` | Delete a job, cancel its pending runs and stop its running runs. |
| `schedule_run_now` | Start an extra run without changing the schedule. |
| `schedule_runs` | Inspect run states and results, newest first. |

`schedule_delete` deletes history by default. Set `keep_history: true` to retain it. The creator can change a job. A person in `scheduling.operator_role` can manage every job. Scopes can allow, ask about, or deny each tool.

The commands `/belle schedule list`, `show <job id>`, `pause <job id>`, `resume <job id>`, `delete <job id>`, and `run <job id>` manage jobs in the current conversation. A person in `scheduling.operator_role` can use these commands across conversations. Matrix accepts `!belle` as the command prefix. A command deletes a job with its history retained. Commands need the Belle engine. Another engine reports that the command is unavailable.

## Request and inspect sessions

With Belle scheduling active, `/belle bg <task>` starts a detached session and returns its session ID. The model can also call `session_start`. The originating turn can end while the task runs.

Before a detached run finishes a required reply, Belle tells it to call the platform posting tool by its harness-visible name. The run must check the tool result. Current tool scopes and audience checks still apply. They keep the original message ID, envelope, and reply placement. Each run has its own request ID.

Optional ambient work may choose silence. Belle stores the final response privately and never automatically posts it. Belle refuses a detached session on a platform without a posting tool. Belle also refuses a detached session that owes a reply when the current scope denies posting.

Set `notify: false` to suppress the separate completion notice. Substantive posts remain available. A continuation always sends a completion notice.

Each run of a detached session, standalone or scheduled, takes one of the `scheduling.max_concurrent_runs` slots. When no slot is free, accepted runs wait in the store. `scheduling.max_pending_sessions` is a limit on the requested runs that wait: the runs of `session_start`, `session_continue` and `schedule_run_now` count toward it. At the limit, each of them refuses a new run with `session_limit_reached`. Runs that the clock starts do not count.

| Tool | Command | Purpose |
| --- | --- | --- |
| `session_start` | `/belle bg <task>` | Start a detached session. |
| `session_continue` | `/belle sessions continue <id> <task>` | Add a run to a retained detached session. |
| `session_list` | `/belle sessions list` | List visible session metadata. |
| `session_get` | `/belle sessions show <id> [cursor]` | Inspect one session without its result. |
| `session_result` | `/belle sessions result <id> [run id]` | Read stored result pages. |
| `session_cancel` | `/belle sessions cancel <id>` | Cancel pending work or request a running stop. |

A detached session is one that Belle starts on its own: a task from `session_start` or `/belle bg`, or a session of a scheduled job. Each detached session owns one conversation on the harness. A `fresh` job makes a session for each run. A `shared` job has one session for all of its runs.

An attached session is the harness session of a thread, a channel or a direct message, and every turn there uses it. The session tools show detached sessions only. `/belle stop` stops a running chat turn.

A session ID does not grant access by itself. `session_result` requires the original requester in a direct conversation on the same platform and workspace. Belle checks that the requester can still read the origin.

Continuation adds a run to a detached session: a task or a session of a scheduled job. OpenCode and Docker Agent advertise `session_continue`. The new run continues the session's conversation. A backend that no longer has that conversation fails the run with `backend_conversation_unavailable` and makes no empty one. Belle reports completion to the original thread.

The requester, or the creator of the scheduled job, or an operator can start it from the origin conversation. The requester can also start it from a direct conversation in the same workspace. Belle checks current membership and scopes again. It refuses busy or expired sessions, the session of a shared job with no run yet, and missing backend conversations. A shared job's session can be continued while the job holds it.

`session_get` lists run IDs, newest first, one page at a time: pass its `next_cursor` as `cursor` for the next page. `session_result` can read an earlier result by its run ID. JiuwenSwarm has no session tools: on JiuwenSwarm, `scheduling.engine: auto` selects the JiuwenSwarm scheduler, and Belle sessions need Belle's scheduler.

Completion notices give the state and session ID without result text or a preview, including in direct conversations. The requester can collect the stored final response in pages from a direct conversation. Continuations keep the original message context and check current permissions again.

A pending cancellation prevents the turn from starting. A running cancellation reports whether Belle sent a stop request. It does not claim the backend stopped until a terminal event reports that outcome. A failed stop request leaves the later outcome as interrupted if no result arrives.

A pending run that `session_cancel`, `/belle sessions cancel` or `schedule_delete` cancels sends no completion notice. The answer to the person who cancelled it lists the run. An operator can also drop a pending run or delete its job in the [TUI](tui.md). Such a run sends its completion notice, so the requester learns of the cancellation.

## Choose a time

| Form | Example `when` |
| --- | --- |
| At one instant | `{"kind":"once","at":"2026-10-23T15:00:00Z"}` |
| After a delay | `{"kind":"once","after_seconds":7200}` |
| Every hour | `{"kind":"every","seconds":3600}` |
| Weekdays at 09:00 | `{"kind":"cron","expr":"0 9 * * 1-5","zone":"Europe/Paris"}` |

Cron expressions have five fields: minute, hour, day of month, month, and day of week. Use `second` for a nonzero second. A wall time or cron expression needs an IANA time zone, unless `scheduling.default_zone` sets one. A time in a daylight-saving gap or overlap needs `dst: earlier` or `dst: later`. Belle refuses an ambiguous time rather than guessing.

Belle applies Debian cron's daylight-saving rule. When neither the minute nor hour field starts with `*`, a skipped local time runs once at the end of the gap. A repeated local time runs once. Other expressions follow real time: they skip the gap and can run in both passes of an overlap.

Known limitation: `cronsim` 2.7 skips a 02:00 run when `Australia/Lord_Howe` falls back by 30 minutes. On 2026-04-05 this affects `*/30 2 * * *` and `0 */2 * * *`. Do not rely on these expressions for that rollback hour until the dependency is fixed.

Runs of one job must be at least `scheduling.min_interval_seconds` apart. A one-shot job missed during downtime runs once after restart by default. A recurring job skips missed runs by default and records them as missed. Set `catch_up: latest` or `all` to run missed recurring times.

## Delivery and permissions

The default target is the thread that created the job. `deliver_to: conversation` selects the top level of that conversation. `creator_dm` selects a direct message to the creator. An address object can name a platform, workspace, conversation, and thread. A new conversation resets the thread to its top level. `thread: new` starts a thread for each run.

`scheduling.delivery` allows targets in the origin's platform and workspace by default. Add rules to allow other workspaces or platforms. Belle checks the rule when a job is created and before each run. It checks that the creator can read the target, the bot can post there, and the target has no unapproved new readers. An audience that grows after approval makes the run fail with `audience_widened` before any post.

The default `questions: refuse` rejects approvals and questions in an unattended run. `questions: ask_target` asks in the delivery target and waits up to `scheduling.question_timeout_seconds`. An approval received after that deadline or after the run ends performs no action.

`session: fresh` gives each run a new session and its own conversation. `session: shared` gives the job one session, so its runs see earlier runs. The runs of a shared job take turns in its conversation. A session and its conversation on the harness stay until the session expires (see [Store and failures](#store-and-failures)).

With `overlap: skip`, Belle marks a due run as skipped while an earlier run of that job runs. `queue` waits for a slot up to `queue_max`. `parallel` starts another run.

## Store and failures

Belle stores jobs and recent runs in `<state dir>/sessions.json`, with a lock beside it. Persist the [state directory](config.md) across restarts. Do not edit the store while Belle runs. An unreadable store stops scheduling. Belle does not replace it with an empty file. Restore access to the file or a valid backup, then restart Belle.

The store uses version 4. Belle refuses a store of version 1, 2 or 3 at start and leaves the file unchanged. Those versions hold records of an earlier session model. To start an empty store, stop Belle and delete the file and its `.bak` copies. The harness conversations of the old records stay on the harness.

A detached session expires `scheduling.session_retention_hours` after its last run ends, 72 hours by default. Two more rules apply to the sessions of a scheduled job only. A standalone session has neither:

- A `fresh` job keeps at most `scheduling.kept_runs_per_job` ended sessions. Its oldest sessions expire first.
- When a person deletes a job without its history, each session of the job expires when its last run ends.

A session with a pending or running run, a completion notice or a recovered final that Belle still owes, or a `shared` job does not expire.

When a session expires, Belle deletes its conversation on the harness, and then its record. Belle tries at start and every five minutes. A failed deletion keeps the record, and the next try deletes it.

A session that the running backend cannot delete stays in the store as `blocked`: a session of another backend, or a backend without deletion. Belle logs a session once when it becomes blocked, and each failed deletion. A Belle process tries a blocked session once. `session_get` shows the error. The control metrics `deleting_sessions` and `blocked_sessions` of the `scheduling` component count the sessions whose deletion waits for a retry and the blocked sessions.

At start and every 30 seconds, Belle takes the runs of a Belle process that stopped. A process on another host counts as stopped 90 seconds after its last heartbeat. A pending run waits for a slot again.

OpenCode can inspect an accepted request by its stored request ID. A stop of Belle leaves OpenCode's runs running, as a crash does. Belle watches a recovered active turn and stores its terminal result without submitting the prompt again. The turn timeout counts from the run's start: Belle stops a recovered run that reaches it, and the run fails. The turn budget does not apply to a recovered run, because the recovery check reports no steps and no cost. Belle then posts the final of a scheduled run to its place. Docker Agent cannot recover a turn: a run ends with the request that streams it, so the run is `interrupted` with an unknown final outcome. Belle does not resubmit an accepted request when its outcome is unknown.

Belle sends a completion notice, or posts a stored final, that a stopped process still owed. A notice becomes `uncertain` when Belle starts to post it, and `sent` with the receipt. A post that fails, or a stop before the receipt, leaves it `uncertain`. The outcome of a scheduled run is `post-uncertain` in the same case. Belle does not post a second copy, and the session then expires by the time rule. When this Belle does not serve the platform of the post, Belle logs an error and posts nothing. The notice becomes `refused`, and a scheduled run keeps its stored final with the error.

`scheduling.kept_runs_per_job` limits the run history of each job. Shared sessions also retain older runs while Belle owes a completion notice or recovered final. A deleted job's history goes with its sessions. `schedule_runs` gives `coverage.kept_since_iso_utc`: the history holds every run and every skipped or missed due time from that instant on. Consecutive failures pause a job after `scheduling.disable_after_failures`. Zero disables this pause. `/belle schedule show <job id>` and `schedule_runs` show the error and recent run states.
