---
name: pr-tracker
description: "Track pull requests and issues whose links are posted in a Slack channel or which a watched list of authors opened, and report what changed since the last time a report was actually delivered. Use when a link to a GitHub pull request, GitHub issue or GitCode merge request is posted in a tracking channel, when asked to track or stop tracking an item, when asked to watch or stop watching what someone opens, when asked for the tracked-changes report, the state of the tracked pull requests, or what moved on them, and when asked any question counting or listing what a channel tracks — how many have merged, which are still open, which were untracked. Covers CI verdicts, CLA and lgtm progress, approval, conflicts and landings across the paired GitHub and GitCode sides of one change."
---

# PR tracker

A channel collects links to pull requests and issues. This skill keeps a store of
everything known about each tracked item, and on request posts what *changed*
since the last report that was actually delivered.

The store and the report answer different questions and do not share a shape. The
store holds the complete current state of every item, refreshed whether or not
anything changed — it is a mirror of the trackers, not a report backlog. The
report names an item if and only if something about it changed. Everything else
follows from that split.

`<skill>` below means this skill's own directory. Resolve it to an absolute path
before running anything; the scripts do not infer it. The store is the exception:
it belongs to the host's own state and not to the skill, and a `--state-file`
pointing inside the skill directory is refused.

## Configuration

**Which repositories a deployment tracks is configuration, and it is not in this
skill.** Nor is who it watches. Both live in one file **derived from the store**:
`<store>` with `.jsonl` replaced by `.config.json`, in the same directory.

Nothing new goes in the prompt, and that is the reason for deriving it: a second
path in a prompt is a second long literal for a caller to retype, and a retyped
path is a mistyped path — the commonest way one of these runs dies. A derived
path adds no literal and is recoverable from the store path at any point. It also
belongs in the state directory rather than the skill directory, because an
operator edits it and the skill directory is replaced wholesale on the next
install.

```json
{
  "schema_version": 1,
  "repositories": {
    "owner/name": {"gitcode": "namespace/project"},
    "owner/other": {}
  },
  "watch": {
    "authors": ["a-login"]
  }
}
```

`repositories` is keyed by the repository on the first tracker, and **the key is
its capitalisation**: an identity key is lowercased, so this is the only place
the owner's own spelling survives to be shown in a report. Its value holds that
repository's properties, today just one.

- **`gitcode` is the pairing** — the project on the second tracker that mirrors
  this repository. The convention is per repository and is derivable from neither
  URL, so it has to be stated. It is what lets one change be reported across both
  trackers at once.
- **A repository absent from `repositories`, or present with no `gitcode`, is
  tracked on the first tracker alone.** That is a supported configuration and not
  an omission: a wrong pairing reports another project's merge as this one's, so
  a pairing nobody has confirmed is left out rather than guessed at. Half a
  report is worth more than a wrong one. Confirm a candidate with
  `--gitcode-project <namespace/project>` on a `report` run before writing it
  down; it overrides the configuration for that run only.
- **Two repositories may not share one project.** That is refused rather than
  noted, because each would then be told the other's merges and both reports
  would look right.
- Keys beginning with an underscore are ignored throughout, so the file can carry
  notes. An unknown key is reported and ignored, never rejected: a newer file on
  an older script should still track and watch what it names.

The file is read by every command that needs a pairing, not only by the watch.
**A file that will not parse stops the run**, including a report: one that
carried on would silently drop every item to a single tracker, which looks
exactly like a deployment that never paired anything. An absent file is not an
error — it means no pairing and nobody watched.

For an older `<store>.watch.json` file, rename it to `<store>.config.json` and
nest its keys under `watch`. The old path is not read.

## Entry points

One script, `<skill>/scripts/pr_tracker.py`, with seven subcommands.

**Two of them register and one of them reports, and only the one that reports
owns the watermark.** `track` and `watch` add rows; `report` decides what a
reader is told and moves the mark that records it. Keeping those apart is not
tidiness: a registering run that advanced the watermark would consume a window
nobody was ever shown, and the report that was meant to deliver it would then
have nothing to say and no way to know it had been robbed.

### `track` — maintain the store

Registers, re-registers and unregisters items. It makes **no network calls and no
status refresh**: it writes identity and registration fields only. That keeps it
fast enough to run inside the turn a link trigger starts, without making the
channel reply wait on GitHub, and it keeps a registration path from ever being
able to corrupt status.

```bash
python3 <skill>/scripts/pr_tracker.py track \
  --state-file "<store>" \
  --channel <channel-id> \
  --via auto_link \
  --message-file -
```

`--message-file -` reads the message body from stdin, which is the form to prefer:
the body arrives inside the turn, so piping it needs no file, no directory to put
one in, and no path that a second command would have to name. A real path is
accepted too, for a caller that already has the message on disk.

`<store>` is the path the deployment's prompts supply; see
`<skill>/references/prompts.md` for the shape and for what changes between
deployments. It is input, and it stays input: nothing this skill prints repeats
it.

`--via` records how the item arrived. The values are the script's own vocabulary,
not any one host's:

```
--via auto_link    a host trigger fired on a link posted in the channel
--via operator     someone asked for this item explicitly
--via reconcile    a scan of channel history found it
```

- **Give it the raw message body**, not a list of URLs you read out of it. The
  script matches links with the same pattern a link trigger matches on. One
  message routinely carries several links and one trigger must register all of
  them.
- `--url <url>` registers an item explicitly (`--via operator`); repeat it for
  several. `--unregister <url|key>` stops tracking one, keeping the row so
  nothing can add it back.
- Registering the same item twice is a no-op except that it records another
  source. This is what makes reposting a missed link a repair rather than a
  duplicate.
- It prints JSON saying what registered, what was skipped as incidental context,
  and what was not trackable. Trust that over your own reading of the message.
- **`--render-table` prints the reply.** After the JSON it renders the items this
  run registered as a Markdown table — URL, Tracker, Repository, Number, Title,
  Author, State, Created, Last activity — every cell read from the store. Relay
  that table; do not compose one of your own, and **do not go to a tracker for
  any of those columns**, because the store already holds all nine. A run that
  registered nothing trackable prints no table and says per URL why not, since a
  table is the statement that these items are now tracked.
- **A newly registered item is mostly `not refreshed yet`, and that is correct.**
  `track` writes identity only, so Title, Author, State, Created and Last
  activity are empty until a `report` run refreshes the row. The table says
  `not refreshed yet` in those cells rather than leaving them blank, because
  "nobody has looked" and "the tracker did not say" are different facts. Report
  it as it is printed. Filling those cells from a tracker would put a network
  call inside the trigger turn, which is the one thing `track` is built to avoid,
  and filling them from a guess is worse.
- `--slack-ts` is the message id Slack issued — ten digits, a dot, six digits,
  `1786613308.937139` — and `--slack-permalink` is the URL Slack issued. Anything
  else is **refused, reported and not stored**: the item still registers, exactly
  as if the flag had been omitted, and the run says so on stderr and in
  `rejected_provenance`. Never derive either, and never format the current time
  into `--slack-ts`. A guessed timestamp is worse than a missing one — it is
  indistinguishable from a real one once written, and `sources[]` deduplicates on
  `(slack_ts, url)`, so it also splits one message into two.
- **Only pass `--slack-ts` if the turn actually carries the message id.** A
  trigger-started turn frequently does not: a host may hold the message id in
  request metadata of its own and never render it into the model's context, in
  which case a prompt that asks for it is asking for something the caller cannot
  obtain, and what it gets back is an invention. Check what the turn actually
  carries before writing the flag into a prompt; where the host does not supply
  it, omit the flag. `--slack-author` is not checked at all: a name has
  no shape that separates a real one from a plausible one, so it is stored as the
  unverified string it is.

### `watch` — register what a list of people opened

The second way an item enters the store. `track` waits for a link to be posted;
`watch` goes and looks, for a named list of authors, in named repositories.

```bash
python3 <skill>/scripts/pr_tracker.py watch \
  --state-file "<store>" --channel <channel-id>
```

**It registers and does nothing else.** It writes no run receipt, and it does
not open the runs sidecar for writing at all — the owner check is the only thing
it reads from there. Run it on its own schedule, ahead of the report, never
inside it: a report turn is long enough already, and a search that fails should
not take the report down with it.

**Who is watched comes from the channel's configuration** — see *Configuration*
above — under its `watch` key. `authors` is the only required key there.

```json
"watch": {
  "repos": ["owner/name"],
  "kinds": ["pull_request", "issue"],
  "state": "all",
  "window_days": 7,
  "authors": ["a-login", {"login": "another", "kinds": ["pull_request"]}]
}
```

**Authors and repositories are separate axes and cross by default**: every author
is watched in every configured repository, which is what a team on a shared set
of repositories wants and keeps adding either one to a single line. An author
entry is a login, or an object with a login and any of the sibling keys
overridden for that one person — so scoping one author to one repository is
`{"login": "…", "repos": ["owner/name"]}` and needs no second structure. A bare
list of names is enough to start and can gain per-person settings later without
rewriting anything. `"enabled": false` stops watching someone while keeping the
record that they were watched.

`watch.repos` narrows the watch to some of the configured repositories; omitted,
it is all of them. Naming a repository there that is not configured is allowed
and searches it on the first tracker, but nothing will pair it.

- **No configuration is a clean no-op**, not an error: a channel that watches
  nobody is a legitimate configuration. **A file that will not parse is a hard
  stop**, because watching nobody and failing to read whom to watch are
  indistinguishable afterwards.
- `--author <login>` watches that login for one run *instead of* the list, so a
  one-off never edits the operator's file and never sweeps the whole list by
  accident. `--repo`, `--kind`, `--state` and `--since` override the same way.
- `--dry-run` searches and reports what *would* register, including how many
  rows the store would gain, without writing. Use it before the first run of a
  new list.
- `--max-new <n>` refuses to add more than `n` items in one run and **lists what
  it declined** rather than truncating quietly. Nothing is lost — registering is
  an upsert, so a later run picks them up.
- **A real run's (never `--dry-run`'s) JSON carries a `card` field**: a
  ready-to-post ```blockkit `card`, not a carousel, naming this run's new-item
  count in `body` and the store's standing tracked/repository/author counts in
  `subtext`. It is built here, not composed from a prompt instruction, because
  nothing about its shape is a judgement call — see `references/prompts.md`'s
  watch schedule, whose whole reply instruction is to relay this field
  unedited.

#### Two sweeps, and why they are asymmetric

For each author, in each repository:

- **open** — everything they have open, with **no date bound at all**. Live work
  is what a watch is for, and a pull request open for three months is the one
  most worth tracking. Because it is unbounded in time, a run that fails loses
  nothing: the next run sweeps the same set.
- **closed** — what of theirs closed inside `window_days`. This is what catches
  an item opened and landed *between* two runs, whose landing is among the most
  valuable lines the report carries and which an open-only watch would never see.

The closed sweep is bounded on **when the item closed**, not on when it was last
touched, so a year-old item with a fresh comment does not register: it has no
delta left to report and would arrive needing immediate retirement.

**The closed sweep is skipped entirely for a login the store has never seen work
by.** A landing from before the channel started watching is not news. "Has seen"
means any row registered by the watch under that login, or any row whose
refreshed author is that person — both are already in the store, so this needs no
extra file. The consequence to know: an author entirely new to the channel whose
first ever item opens and closes between two runs is missed once.

Everything else in history is deliberately not registered. A watch is not a
backfill, and there is **no first-run mode** beyond the rule above: the first run
and the thousandth run do the same thing, so no hidden state decides which
behaviour a given run gets.

> **`window_days` is a registration window, not a reporting one.** It decides
> which items enter the store and has no bearing on which of them the report
> names. That is watermark-based and independent: an item registered today is
> reported whenever it next moves, however long that takes. Reading the two as
> one is the easy mistake here and it is a real bug — it would make the watch
> look like a second, competing report.

#### What it costs and what goes wrong

The search endpoint is not the API the refresh uses. It has **its own budget, a
much tighter one on a minute-long window**, and it stops at 1000 results per
query however many pages are asked for. Two requests per author per repository
per run, and:

- **A rate limit** stops the pass where it is. What was already found is still
  registered, the authors not reached are named in the output, and the run exits
  non-zero. Nothing is lost: registering is an upsert and the open sweep has no
  floor to slide past anyone. Where the budget refills within about a minute the
  run waits it out **once**, then gives up rather than sleeping through a second
  wall and stranding the caller.
- **A login the endpoint will not search** is reported per author and never
  folded into "no results". This matters more than it sounds: a typo, a renamed
  account, a suspended one and a machine account addressed by the wrong spelling
  all come back identically, and every one of them otherwise looks exactly like
  someone who happened to open nothing. The run exits non-zero so a schedule
  notices. **A machine account is searched under its application spelling, not
  its bare name** — the bare name is refused as a user that does not exist.
- **A result cut short by the cap** is reported as cut, never presented as the
  whole answer.

#### GitHub only, and why

The watch is **GitHub-only**, and this is a property of the trackers rather than
a gap. The other tracker does support filtering by author — but on *its own*
identities, which are a separate namespace: a login from the first tracker is
refused there as a user that does not exist, and there is no mapping between the
two. A watch list is a list of one tracker's logins, so it can only be applied to
that tracker. Registering the other side directly would also create a
provisional row that the pairing logic then has to collapse into the first —
manufacturing exactly the split identity the skill spends effort undoing. The
second side of a watched item arrives anyway, through the pairing discovery every
refresh already does.

### `report` — refresh, diff, render

Refreshes every active row from both trackers, writes the new facts into the
store, diffs them against what the reader was last told, and renders the report
to stdout and to a file. It never delivers, and by default it does **not**
advance the watermark: it leaves a pending receipt and prints, on stderr, the
`commit` command that applies it.

```bash
python3 <skill>/scripts/pr_tracker.py report \
  --state-file "…/<channel-id>.jsonl" --channel <channel-id>
```

The file lands in the `.run` directory beside the store. The script replaces
the store's `.jsonl` suffix with `.run` and prints the result on stderr. `--output` overrides the path and is rarely
worth reaching for: the derived one is the only one a later command can work out
for itself.

**Nothing may be written into the `.run` directory beside the store on the same command line as
`report`.** It empties that directory as its first action, so a `>` into it — on
`report` itself, or on anything piped from it — makes a file `report` then
deletes while it is still being written, and the write is lost without anything
reporting an error. There is nothing to gain from one: `report` already prints
the finished report on stdout and writes the same text to `report.md` there, so
nothing has to be extracted from its output or saved out of it. `report` refuses
such a command line rather than performing it; `--output` is how a report goes
somewhere else.

`--full` adds the roster, `--repo owner/name` narrows to one repository and keeps
its own watermark.

**The refresh is bounded, in two ways, and both are about finishing.** Every row
is one or more requests to a tracker and the run is their latency added up, so
the cost grows with the store while a caller's patience does not.
`--refresh-workers` (12) refreshes that many items at a time, which is what
makes a run of any size finish at all. `--max-seconds` (180) is the backstop:
when the budget is spent the run stops refreshing, renders what it has, and says
in the brief and in *Coverage and gaps* how many items it never reached. A row
already in flight finishes, so a run can exceed the budget by one request's
timeout and no more. Those items are
left out of every section rather than reported from stored facts, and their
watermark is not advanced, so the next run reports whatever changed on them.

- **A partial report is stated, never quiet.** A run that stopped early produces
  a short report, and a short report is exactly what a quiet week produces. The
  notice is in the channel message rather than only in the thread for that
  reason: it is the one line that tells the two apart.
**The second tracker has a rate limit and it is the binding constraint on a
run, not the worker count.** It allows a fixed number of calls per user per
minute; it states that number nowhere except in the text of the refusal, sends
no rate-limit header on the way to it and no `Retry-After` at it, and no
credential raises it — the threshold is per user, an anonymous caller is one
user, and everything reading it from one address shares one budget. So the
count is kept on this side: calls are paced below the ceiling,
`--gitcode-requests-per-minute` (40) is what that pace is, and a refusal
arriving anyway spends the whole window rather than one call, so every worker
waits it out instead of walking into the same wall one item at a time. A wait
that would run past `--max-seconds` is not taken; the item reports the ceiling
as its refresh error and the next run retries it.

Raising `--refresh-workers` does not raise this. Past the point where the
tracker is the limit, more workers buy nothing and cost refreshed items.

**What the refresh costs per item is the number that decides how large a store
can get**, so two things are deliberately kept off it:

- **The second tracker is read per project, not per item.** Its listing carries
  the whole merge request — state, merge date, head sha, labels, mergeable — a
  hundred at a time, and one page of it costs about what two single merge
  requests cost. So a run reads the listing once for each project and answers
  every row of that project from it. A store too small for the pages to pay for
  themselves reads no listing and fetches each row as before; a row the listing
  does not reach still gets its own request, so this is never a narrowing of
  what is reported. `--max-pages` (6) bounds how far the listing is read.
- **An item with no merge request is not searched for on every run.** Finding
  out that a pull request has no mirror costs the whole listing, and the finding
  is written into the row with the date it was made. `--pairing-recheck-hours`
  (6) is how long it is read back before the search runs again — it is aged out
  rather than kept, because the mirror is created some time *after* the pull
  request opens and a finding kept forever would hide it arriving.

- **A `report` killed by a caller's timeout must not be reissued unchanged.** It
  will spend the same time on the same store and be killed again, and each
  attempt costs the whole budget of whatever turn it is running in. Lower
  `--max-seconds` below that timeout so the run stops itself and delivers a
  partial report, or raise `--refresh-workers` so it has less to stop for.
  `--max-seconds 0` removes the bound and is for an operator watching the
  command, never for a scheduled run.

**A store that does not exist stops the run.** `report` refuses before it takes
the lock, creates the runs sidecar or reads anything, and it exits non-zero. That
is deliberate and it is not a convenience check:

- A report against a missing store is not an empty report. It is a report about
  the wrong file, and it renders as a complete, plausible, entirely false "0
  tracked" that no reader can tell from a quiet week. The run exits 0, so nothing
  downstream notices either. It is the same defect class as a check that passes
  because it inspected nothing, and it is worse than a crash for the same reason:
  the output is well-formed.
- The overwhelmingly likely cause is the path, not the channel. A store goes
  missing when a path is mistyped, when a variable in it expanded to nothing, or
  when the store was moved — not because a busy channel emptied itself.
- **Do not reach for `--allow-missing-store` to get past it.** Read the message
  first: it says whether the directory exists at all and whether this channel's
  sidecars are sitting beside the store, which is usually enough to identify the
  real path. The flag exists so that "report against nothing" can be *stated*,
  not so the refusal can be silenced.

A genuinely new channel is not shut out by this, and needs no flag: `track` and
`watch` both create the store, so a channel becomes reportable by registering
something. That is the order every channel already follows, and there was never
anything for a report to say before the first registration.

**A `--repo` matching no repository stops the run**, the same way a missing store
does and for the same reason. The filter is exact, so a value naming nothing
drops every row and renders a store full of open work as a channel with nothing
to say — which is exactly what a genuinely quiet week renders as, and no reader
can tell the two apart. The refusal names every repository the store is
configured for or has a row from; `--repo` takes one of those, spelled
`owner/name`. It is not a channel name, not a project's nickname and not a
deployment's name for the work, and inventing one costs more than the run: the
flag also names the watermark stream, so a value nothing matches opens a stream
with no history and the report says `since the first run` on a channel that has
been reporting for weeks.

A `--state-file` written in terms of an environment variable that is *set but
empty* is refused too, by name, in every subcommand — see *Rules*. That is the
other half of the same failure, caught one step earlier.

`--commit-after` applies the receipt in the same process, immediately after the
report has been rendered, written to its file and put on stdout. It then prints
**no epilogue**: there is no command to run, so there is none to print. Instead
it writes the same JSON `commit` writes — `committed`, `items`, `store` — to
**stderr**, since stdout is the report text and a caller can no longer infer that
a commit happened from having run a second command.

- **Opt-in, and it must stay opt-in.** It trades a real guarantee for a removed
  step: the watermark moves once the report *exists*, not once a reader *has* it.
- Take that trade only where the guarantee was unavailable anyway. **Which
  ordering is right depends on the host**, and on one thing about it: whether a
  turn can observe its own delivery. Where the host sends the reply only after
  the turn has ended, no run can confirm delivery from inside itself, so
  committing late buys nothing while still leaving a long command to transcribe —
  use `--commit-after` there. Where the caller can see the send succeed or fail,
  commit late and keep the guarantee. Establish which your host does before
  choosing; `<skill>/references/host-notes.md` lists the host checks to make.
- Nothing is committed by a run that failed. A render that raises never writes a
  receipt, and a report file that cannot be written is a hard stop *before* the
  commit: no file means no delivery, so the run stays pending and the next run
  repeats the delta.

### `commit` — advance the watermark, after delivery

```bash
python3 <skill>/scripts/pr_tracker.py commit \
  --state-file "…/<channel-id>.jsonl" --channel <channel-id> --run-id <printed run id>
```

**Deliver first, commit second, and never the other way round.** The script holds
no Slack credential, so delivery is the model's action, and the two commands
exist so that the watermark moves only once the reader actually has the report. A
crash between them repeats a delta; committing without delivering loses one. The
split is chosen for that reason and no other: duplicated news is recoverable by a
reader, lost news is not.

This is the mechanism of the human-triggered path, and it stays: a caller who can
observe a failed delivery and decline to commit is the whole reason the split
exists. It is also how a stalled run is finished by hand after an `audit` or a
recovery. `--commit-after` is for the path that has no such observer.

### `query` — answer a question about the store

Read-only, writes nothing, takes no lock and makes no network call. It exists so
that a question about what is tracked is a subcommand rather than an improvised
`grep`.

```bash
python3 <skill>/scripts/pr_tracker.py query \
  --state-file "<store>" --channel <channel-id>
```

With no filters and no `--format` it prints a census: how many merged, and the
spread of `gh_state`, `lifecycle`, `kind` and the derived status. That is
usually the whole answer, and it costs no flag to guess at.

- **Filters are named after the store's own fields**: `--merged` / `--unmerged`,
  `--gh-state`, `--lifecycle`, `--kind`, `--status`, `--repo`. Repeat any of the
  list-valued ones to widen it. `references/store-schema.md` explains each field;
  the flags use its vocabulary on purpose.
- `--format count` prints the number on its own line, with the store and the row
  count under it. `--format table` prints the nine-column table (`--limit`
  bounds it). `--format json` is the same rows for a caller that will process
  them.
- **Every output names the store it read and how many rows were in it**, even
  the one-line count. That is what makes a number checkable, and it is not
  decoration: it is the only thing separating "nothing matched" from "nothing was
  read".
- **A store that does not exist exits `2` and counts nothing**, the same
  refusal `audit` makes. A count over a file that was never opened is not an
  answer of zero.

**Never answer a question about the store with `grep`, `jq`, `wc` or a shell
pipeline.** Two mistakes are indistinguishable from a true result there and both
are easy to make: a pattern naming a field that does not exist matches nothing,
and a path that does not exist matches nothing. Each prints `0`, and where a host
reports every command as having succeeded, nothing else in the output disagrees.
A wrong answer that looks right is then followed by variations on the *path*
rather than the pattern, which is how a store path gets mistyped into a loop.
`query` cannot produce either failure silently.

**`gh_state` never holds `merged`.** The first tracker reports a merged pull
request as `closed`, exactly like one closed unmerged; the merge marker is
`gh_merged_at`, and the second tracker's is `merged_at`. `--merged` reads them
all. This is the single most common wrong assumption about this store, so if you
are about to match on a state field, read
`<skill>/references/store-schema.md` first.

### `title` — cache a rendering

`title --key <key> --rendered <text>` caches the output-language rendering of a
title whose source is in another language. Rendering, never dropping, is how a
language failure is fixed.

### `audit` — find provenance that cannot be trusted

```bash
python3 <skill>/scripts/pr_tracker.py audit \
  --state-file "…/<channel-id>.jsonl" --channel <channel-id>
```

Reports two kinds:

- `malformed_slack_ts` — entries whose stored `slack_ts` is not a timestamp.
  These predate the check; nothing can write one now.
- `refused_slack_ts` — registrations that arrived with a fabricated `slack_ts`,
  had it dropped and registered without it. The row is honest; whatever is
  passing the flag is not, and that is where to fix it.

**Three exit codes, because this is meant to be used as a gate:** `0` the store
was read and is clean, `1` the store was read and something was found, `2` there
was no store to read. The third is not a variant of the first:

- A clean verdict over a file that was never opened is the same defect class the
  audit itself looks for — a check that passes because it inspected nothing. An
  absent store most often means the path is wrong, and a `0` there sends a caller
  away satisfied about a store it has never seen.
- The JSON says which happened as plainly as the exit code does. `audited` is
  `false`, and the findings keys are **absent rather than empty**: a
  `malformed_slack_ts: []` reads as a clean result to every reader there is, so
  it is not printed. The output instead carries what is around the path, the same
  diagnosis `report` gives for the same absent store.
- **There is no `--allow-missing-store` here**, and that is a deliberate
  difference from `report` rather than a gap. `report` has the flag because a
  report over an empty store is an artefact someone may genuinely want delivered
  and can *state* that they want. There is no equivalent artefact here, so there
  is nothing for a flag to authorise.
- A genuinely new channel is not shut out and needs no flag, exactly as with
  `report`: `track` and `watch` each create the store, so a channel becomes
  auditable by registering something — and before the first registration there
  was never any provenance to audit.

Strictly read-only — it takes no lock, creates no file and changes nothing. That
holds for the absent-store path too: it does not bring the store, the
`.runs.json` or even a `.lock` into existence in whatever directory a bad path
named.
**It repairs nothing, on purpose**: a value that was invented once cannot be
recovered into the real one, and overwriting it would swap one unverifiable
claim for another. Either leave a malformed entry and treat its `slack_ts` as
unknown, or, where provenance matters, `track --unregister <key>` and have the
link reposted so it registers again with real metadata.

## Answering a question asked in the channel

Not every use of this skill is a scheduled run. Someone asks "how many of these
have merged?", "which ones are still open?", "what happened to the one I posted
yesterday?" — a turn with no schedule behind it and no report to render.

**Answer it with `query`, in one command, and relay what it printed.** The whole
census is one invocation with no filters; a narrower question is one filter. Do
not read the store with `grep`, `jq` or a pipeline, and do not open it in an
editor to count by eye. The reasons are under `query` above.

**The store path comes from the prompt, and only from the prompt.** A channel's
prompt states it — for the registering trigger, for the schedule, and for
interactive use. Where the turn carries that path, use it exactly as written,
character for character.

- **Do not search for the store.** No `find`, no `ls` over a home directory, no
  guessing at a workspace layout. The path is configuration this skill does not
  hold and cannot derive; a path arrived at by searching is a path nothing
  checked.
- **Do not retype it from memory or rebuild it from parts.** Copy it. Long
  absolute paths are where transcription errors live, and one wrong character
  produces a file that does not exist rather than an error that says so.
- **Never vary the path to make a command work.** If a command found nothing,
  the next thing to run is not the same command with a different spelling of the
  directory. Re-read the path the prompt gave and compare it to the one that
  ran. Repeating a command with the path edited each time is a loop, and it ends
  with a turn aborted rather than an answer.
- **Where the turn carries no path at all**, say so and ask for it. That is a
  short, correct answer. Searching the filesystem for a plausible candidate is a
  long one that can be confidently wrong, and a store found that way may belong
  to a different channel entirely.
- `query --channel <channel-id>` refuses a store that records a different owner,
  which catches exactly that mistake. Pass it.

**Read `<skill>/references/store-schema.md` before making any claim about a
field.** Most wrong answers here are not arithmetic; they are a field that was
assumed into existence. There is no `status` field in the store, and `gh_state`
never holds `merged`.

## Workflow for a report

Two orderings, and which one applies is decided by the prompt that triggered the
run, never by the model at the time. A caller who can see whether the reply
landed commits **late**, in a second command; a caller who cannot commits inside
`report` with `--commit-after`. Both prompts are in
`<skill>/references/prompts.md`; they diverge on purpose and must not be
reconciled.

1. Run `report` with the store path the channel's registering prompt names. **The
   prompts must name the same path**; see `<skill>/references/prompts.md`.
2. If it exits non-zero, report the failure and stop. A missing `GITHUB_TOKEN`
   is a hard stop by design, not something to work around: anonymous GitHub
   access runs out of rate limit partway through a refresh, and a half-refreshed
   report looks exactly like a complete one. Say what went wrong; do not paste
   store paths into the channel — they are configuration the prompt supplied, and
   a reader can do nothing with them.
3. Check the finished report language when a host-side checker is available.
   This skill ships no checker and does not require another skill. If no checker
   is available, say the check was skipped.
   If a title fails, cache a rendering with `title` and produce the
   report again — except after `--commit-after`, where the run is already
   committed and a second report would consume a second window: cache the
   rendering for the next run, post what you have, and say the title was not in
   the output language. Never skip the check silently.
4. Post the text exactly as rendered, **including *Source and registration*** —
   it names the store that was read and how many rows it held, which is the only
   way a misconfigured store shows up as a wrong report rather than as silence.
   It names the store's file and never its path: a path is input, supplied by the
   prompt, and putting it in the report would publish a host's directory layout
   to the channel on every run. Post the report even when it says nothing
   changed.

   Exactly as rendered includes **every** HTML comment in the middle of it. Each
   is a message boundary a host may split on: the brief above the first goes to
   the channel, the delta tables follow in that message's thread, the roster
   follows them, and the prose comes last. Tidying any of them away is invisible
   in the text and collapses the report — dropping the first posts the whole
   thing, tables and all, into the channel, and dropping a later one puts two
   sections back on one Block Kit budget, which is what costs both of them their
   formatting on a long run.

   A run without `--full` renders no roster, so one of those boundaries has
   nothing between it and the next. That is expected: the host drops an empty
   piece rather than posting a blank message, and the marker is still emitted.
   Do not remove a marker because the run it was rendered for had nothing to put
   after it.

   **A long roster is rendered in numbered pages — `*Roster (1/2)*`,
   `*Roster (2/2)*` — with a boundary between them, and those headings and
   boundaries are load-bearing.** Each page is sized to fit one message's
   rendering budget, so merging two of them back together produces a single
   table too large to render and the whole message arrives as raw pipe
   characters. This has happened: a relay that reproduced every row but dropped
   one heading and the marker above it undid the paging completely. Copy the
   report out whole, in order, headings and boundaries included, and never
   summarise, re-wrap or re-assemble it. If the report is too long to reproduce
   faithfully, say so plainly rather than posting a repaired version of it —
   a report that says it was truncated is recoverable and one that looks
   complete is not.
5. Only then, `commit` with the run id the report printed — unless the run was
   given `--commit-after`, in which case it is already committed and step 5 does
   not exist. Nothing is printed for you to run, and there is nothing pending to
   run it against.

## Rules

- **Read-only on both trackers, absolutely.** Every call is a GET. Never comment,
  close, approve, merge, label or push — including on an item the report itself
  calls stale. That is a line for a human to act on, not an action to take.
- **GitHub through `GITHUB_TOKEN` from the environment**, read via `--token-env`.
  **Never shell out to `gh`**: the point of naming an environment variable is that
  the run uses exactly the credential the operator put there. `gh` uses whatever
  grant it is logged in with, which is a different credential, chosen by nobody at
  the point of the call and usually broader. Which binary got invoked is not an
  acceptable way to decide what access a report runs with.
- **Never print or persist a token**, in the report, in the store or in a log
  line.
- **GitCode is read anonymously.** It needs no credential.
- **No Slack credential at all.** The principle: borrow what the host already
  has, supply only what it lacks. A host that delivers these reports into Slack
  already has a Slack client of its own, so a second one inside a skill would be
  a duplicate route to something the host has; GitHub has no such route, so it is
  supplied. A Slack token may well be reachable from a skill script — not using
  it is a scope decision, not a capability limit.
- **Nothing here schedules anything.** A report is always triggered by a prompt.
  Whether that prompt is pasted by a person or handed to the run by the host's
  own scheduler is the host's business: this skill neither creates nor modifies a
  schedule, and `--commit-after` is a flag a prompt may carry, not a scheduler.
- **Working files belong in the run directory beside the store**, the path made by replacing `<store>`'s `.jsonl` suffix with `.run`,
  which `report` empties and creates for itself and whose path it prints. Use the
  path as printed; do not rebuild it and do not name a directory of your own.
  Two places it must not be. Not the skill directory: it is shared by every run,
  and hosts that install skills from a source of record replace it wholesale on
  the next install. And not a `mktemp -d`, for two reasons that both cost real
  reports — each command runs in its own shell, so the variable holding a random
  directory is gone by the next command and the directory it named cannot be
  found again; and where the working directory is shared between sessions rather
  than per-session, anything a failed run leaves in it is picked up by an
  unrelated later run.
- **Where the prompt writes the store path in terms of an environment variable,
  the variable is already set. Never export it, assign it, or re-establish it.**
  Use it exactly as the prompt wrote it. Two reasons, and the second is why this
  has cost real reports:
  - Where each command runs in its own shell — which is the usual arrangement —
    an assignment is gone by the next command, so it cannot help even when it is
    correct.
  - An assignment that comes out empty is *silent*. `$VAR/some/path` with `VAR`
    set to nothing expands to `/some/path`: still absolute, still plausible,
    pointing at a directory that does not exist. No dollar sign survives for a
    check to catch and nothing errors. The store is then not found, and before
    the refusal below existed the run rendered a perfectly formed report saying
    that nothing was tracked.

## What registration does not cover

Two routes in, and each has its own blind spot. They overlap usefully — the watch
covers people, the link trigger covers anything anyone wants to raise, including
work by someone not on the list — but neither is complete and together they are
still not.

The **author watch** sees only the people on its list, only in the repositories
it is pointed at, and only work that is open now or closed inside the window. A
contributor added to the list today brings their open work with them and not
their history. Everything else has to arrive by a posted link.

The **link trigger** is whatever the host offers for links posted in a channel,
and such a trigger has real limits. Establish which of these apply
before trusting coverage, because all of them are common: it may fire **once per
message** however many links that message carries; it may see **only messages
posted by people**, so a link posted by a bot or an app integration never
triggers it; and it may see **only new messages**, so a link added by editing an
existing one never registers. A message that arrives while the service is down is
not replayed either, and nothing detects the omission, because the item never
entered the store to have a watermark.

This is a known, bounded limitation, not a defect — but it is never to be
softened. The report carries `Registration: link trigger only` in *Source and
registration* on every run, and says in *Coverage and gaps* that anything posted
during downtime is missing until it is posted again, so that a quiet channel is
not mistaken for a quiet week.
Three ways out, in order: repost the link (it upserts), ask for it explicitly
with `track --url`, or reconcile against the channel history.

> **Reconciling needs the host's own channel-history capability, and that
> capability may not be offered on every path this skill can be invoked from.**
> Where it is not, a history scan returns an empty result indistinguishable from
> an empty channel — a silent wrong answer rather than an error. Before relying on
> a reconciliation pass, confirm on a channel known to be non-empty that the scan
> actually returns something. Record which invocation paths offer history
> in the deployment configuration.

## References

- `<skill>/references/prompts.md` — generic templates for registration, author
  watch, and human-triggered or scheduled reports. Replace the placeholders
  with your channel and store values before use.
- `<skill>/references/host-notes.md` — host integration checks. Confirm these
  properties on your deployment before enabling scheduled reports.
- `<skill>/references/report-shape.md` — sections, impact ranking, edge cases.
- `<skill>/references/store-schema.md` — the row, the sidecars, the lifecycle,
  how to read the store and how to fix it by hand. **Read it before making any
  claim about a field**, and in particular before filtering on one: the three
  fields that look like an item's state mean three different things, and the
  merge marker is not the one most readers reach for.
- `<skill>/references/ci-flake-signatures.json` — configuration, holding one
  project's signatures as an example. Replace them with your own and extend when
  a new flake appears. Signatures match failure text, never test names, because a
  flake's victim rotates. A path given to `--flake-signatures` and not found
  **stops the run**, and a signature file that exists and cannot be read stops it
  whether it was named or shipped: losing the signatures leaves every red build
  reported as matching nothing recorded, which reads exactly like having checked.
  A signature set that can classify nothing — absent shipped file, empty list,
  entries with no `all_of` — is reported on stderr rather than passed over.
