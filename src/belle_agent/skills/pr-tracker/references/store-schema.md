# The store

One JSONL object per line, plus three sidecars and a scratch directory, all
derived from the ledger's own name:

```
<channel-id>.jsonl            the ledger        (--state-file names this one)
<channel-id>.runs.json        run bookkeeping   (written only by report/commit)
<channel-id>.config.json      repositories and the author watch (only a human writes it)
<channel-id>.jsonl.lock       the lock file
<channel-id>.run/             this run's working files — emptied at the start of
                              every report, keeps nothing across runs
```

The prompt states one path, not five; the rest are derived from it, the
ordinary way a lock file and its sidecar are named after the file they belong
to. Derivation is deliberate rather than incidental: every additional path a
prompt states is another long literal for a caller to retype, and a retyped path
is a mistyped one. The configuration is derived for that reason and for one
more — it is edited by a person, so it must sit in the state directory, which
survives a skill upgrade, rather than in the skill directory, which does not.

**The `.run` directory is scratch, not state.** It holds one file today,
`report.md`, and it is derived rather than passed for a reason the sidecars do
not have: each command of a run executes in its own shell, so a directory made
by the first command with `mktemp -d` cannot be named by the second. The report
is written by one command, checked by the next and read by the third, so the
path has to be one all three can work out for themselves. It is emptied when
`report` starts rather than when it finishes, because a run that fails never
reaches a finish, and a stale `report.md` left beside the store reads exactly
like a fresh one to the step that checks it.

**Only `report` and `commit` write `runs.json`.** `track` and `watch` add rows
and never touch it, which is what keeps the watermark out of a registering run's
reach; `watch` reads it for the owner check and for nothing else.

## Where it lives

```
--state-file <path>                              whatever the prompt names — wins
$XDG_STATE_HOME/pr-tracker/<channel>.jsonl    script default
~/.local/state/pr-tracker/<channel>.jsonl     script default, XDG unset
```

The script defaults to XDG because it should travel: `~/.local/state` means the
same thing anywhere the XDG base-directory convention is followed, and a
host-shaped path compiled into the script would bake one deployment into a skill
with no reason to care. Where `XDG_STATE_HOME` is unset, the `~/.local/state`
fallback is the branch that runs.

A deployment should nonetheless override that default with a path inside the
workspace its host already manages — the prompts in `prompts.md` show the shape —
for two reasons that are only discoverable the hard way. **Sandbox
reachability**: where a host sandboxes file access, the roots it grants are
typically the workspace it manages, the project and the working directory, and
not arbitrary sibling directories. A store outside those works fine on a host with
sandboxing off and breaks the day it is switched on. **Backup coverage**: a backup
that covers a host's own data directory frequently does not cover
`~/.local/state` — and losing this file does not lose a little history, it
re-registers and re-reports the channel's entire backlog. Check both on the host
you are installing to rather than assuming either.

The path itself is **input**: prompts supply it because the script cannot find
the store without it. Nothing the skill emits repeats it. The report names the
store by file name only, which is enough to see that the wrong one was read and
carries nothing about the host.

A `--state-file` inside the skill's own directory is refused. That directory is
shared by every run, and hosts that install skills from a source of record replace
it wholesale on the next install.

## One store per channel

Repository is a column, not a file. The registering prompt cannot know the repo
in advance — it is only discoverable after the URL is parsed — and the watermark
is a property of a report stream rather than of a repository. A per-repository
view is `report --repo owner/name`, a filter over the one store, and it keeps its
own bookkeeping under `streams` so two filtered runs do not consume each other's
deltas.

## A row

**Identity and registration** — written by `track`, no network calls:

| Field | Notes |
| --- | --- |
| `key` | the primary key: `github/<owner>/<repo>#pr<N>`, `…#issue<N>`, or `gitcode/<project>#mr<iid>` when no GitHub origin is known yet |
| `aliases[]` | keys that resolved into this row, so a re-posted GitCode link lands here |
| `url` | canonical, normalised |
| `kind` | `pull_request` \| `issue` \| `other` |
| `tracker`, `repo`, `number` | the tuple the key is built from |
| `sources[]` | `{via, slack_ts, url, at}`, deduplicated on `(via, slack_ts, url)` — never on `slack_ts` alone, because one message carries several links, and never without the route, because an item the watch found *and* someone posted a link to arrived twice by two routes and both are true |
| `sources[].watch_author` | present only on a watch registration: the login it was found under. It is the record that this channel has seen work by that person, which is how the watch tells a login it has swept before from one it is meeting for the first time |
| `sources[].slack_ts` | `<10 digits>.<6 digits>`, or `null`. Only ever a value Slack issued: a value of any other shape is refused at registration, so `null` means unknown and never "guessed" |
| `sources[].slack_ts_rejected` | present only when a registration supplied a `slack_ts` that was refused. Diagnosis, not provenance — it is outside the dedupe key, so such an entry collapses with the `slack_ts: null` entry an omitted flag would have written. `audit` reports it |
| `first_seen_utc`, `slack_permalink`, `slack_author` | provenance, never identity. `first_seen_utc` is derived from `slack_ts` only when one was accepted; `slack_author` is unverified, having no shape to check |
| `lifecycle` | `active` \| `terminal_pending` \| `retired` \| `ignored` |
| `reopen_count` | how many times it came back |

**Tracker facts** — written by `report`'s refresh phase, every run:
`title_source`, `title_rendered`, `author_login`, `created_at`, `head_sha`,
`gh_state`, `gh_merged_at`, `github_labels`, `conflicted`, `conflicted_label`,
`conflicted_gitcode`, `draft`, `closes`, `mr_iid`, `mr_state`, `merged_at`,
`mr_head_sha`, `mr_url`, `mr_source_branch`, `gitcode_labels`, `cla_ok`,
`approved`, `lgtm_count`, `lgtm_logins`, `ci_state`, `ci_verdict`,
`ci_failure_hint`, `ci_head_sha`, `ci_artifact_url`, `ci_classified_sha`,
`ci_failing_test_count`, `ci_flake_signatures`, `stale_ci`, `comment_count`,
`last_activity_utc`, `last_refresh_utc`, `refresh_ok`, `refresh_error`,
`unknown_labels`, `gone`, `pairing`, `pairing_project`, `pairing_checked_utc`.

**None of these exists until a `report` run has refreshed the row.** `track` and
`watch` write identity and registration only, so a freshly registered item has a
URL, a repository and a number and no title, no author and no state at all. A
field that is absent means nobody has looked yet; it does not mean `open`, and it
does not mean unmerged. `last_refresh_utc` is how the two are told apart, and it
is the reason that field is worth naming rather than treating as bookkeeping.

An `ignored` row is never refreshed either, so its facts are whatever the last
run before it was ignored saw — or nothing at all, if it was ignored before its
first refresh.

**The pairing fields are findings, and each is read back on the terms it was
found on.** `pairing` is `github-pr-branch` once the merge request is known,
`none` once a search established there is not one, or `unmapped` when the
repository has no project configured at all.

| Field | Read back? |
| --- | --- |
| `mr_iid` | always. A merge request does not stop being the one that mirrors this pull request, so it is looked up once ever and every later run goes straight to it |
| `pairing: none` | while `pairing_checked_utc` is inside `--pairing-recheck-hours` and `pairing_project` is still the configured project. Establishing it costs the whole listing, so an unread finding is that cost on every run forever; a finding kept forever would miss the mirror being created later, which is the one event the search exists to catch |
| `pairing: unmapped` | never. It is a fact about the configuration, not about the item, and it carries no date for exactly that reason — the first run after the configuration gains the mapping must stop saying it, with nobody editing the store |

A `none` with no date is one written before findings were dated: it is
established again rather than believed, which is how a store upgrades itself.

**Reported mirror** — written when the run commits, by the `commit` subcommand
after delivery or by `report --commit-after` as the report is produced:
`reported_status`, `reported_conflicted`, `reported_lgtm_count`,
`reported_approved`, `reported_ci_verdict`, `reported_comment_count`,
`reported_head_sha`, `reported_cla_ok`, `reported_stale_ci`,
`reported_ci_failure_hint`, `observed_through_utc`, `last_mentioned_run`.

`status` is **derived, never stored**: two sources of truth for one thing is how
a store and a report start disagreeing.

## The three fields that look like the item's state

They are easy to confuse and they answer three different questions. Confusing
them is not a theoretical risk: a reader who counts `status: "merged"` over this
store counts nothing, because there is no `status` field and no field of any
name ever holds the string `merged` on the first tracker. The count comes back
`0`, and `0` is a completely plausible answer to "how many have been merged".

| Field | Whose fact it is | Values | What it does **not** say |
| --- | --- | --- | --- |
| `gh_state` | the first tracker's | `open`, `closed` — and nothing else, ever | Not whether it merged. A merged pull request is `closed` here, exactly like one closed unmerged |
| `gh_merged_at` | the first tracker's | the merge time, or `null` | Nothing about the second tracker, and nothing for an issue, which cannot merge |
| `merged_at`, `mr_state` | the second tracker's | the merge time / `merged`, `closed`, `opened` | Nothing about the first tracker's own state, and nothing at all for an item with no mirror |
| `lifecycle` | **this skill's**, about its own tracking | `active`, `terminal_pending`, `retired`, `ignored` | Nothing about the tracker. `retired` covers merged *and* closed-unmerged; `ignored` says a human took the item out of the report |

**`gh_merged_at` is the merge marker on the first tracker, and it is the only
one.** It is written for pull requests only, by the same refresh that writes
`gh_state`, and it is what separates a change that landed from one that was
abandoned — a distinction `gh_state` cannot make and never could.

**So: how many are merged?**

```bash
python3 <skill>/scripts/pr_tracker.py query --state-file "<store>" \
  --channel <channel-id> --merged --format count
```

Which counts rows whose `gh_merged_at` is set, or whose `merged_at` is set, or
whose `mr_state` is `merged` — the marker wherever it is, on either tracker.
`query` with no flags at all prints the same number alongside the spread of
every field in the table above. **Count with `query`, never with `grep`**: a
`grep` for a field name the store does not have returns `0` and looks exactly
like a real answer, and a `grep` against a path that does not exist returns `0`
too. `query` refuses an absent store and names the file and row count it read in
every output it produces, so neither mistake can pass as a number.

**`status` is not on that list**, because it is derived rather than stored — see
just above. It is also narrower than the question: the derivation calls an item
`landed` on the *second* tracker's merge alone, since a close on the first
tracker routinely is not a landing here, so a pull request merged on the first
tracker with no mirror derives as `closed`. That is right for the report and
wrong for counting merges, which is why `query --merged` reads the markers
directly rather than the derived status.

## Why every reportable field is stored twice

One status column cannot distinguish "changed, and the reader has been told" from
"changed, and the reader has not been told yet" — and those diverge the moment a
run refreshes successfully but fails before delivery. The delta is
`field != reported_field`. That is the entire delta engine, it is inspectable by
reading the file, and it self-heals: a run that dies before delivery leaves
`reported_*` untouched, so the next run reports the same change rather than
swallowing it.

**A one-directional check breaks this, and not only by staying quiet.** A mirror
is written by the receipt, and the receipt only covers rows the report named. So
a field that can differ from its mirror without producing a change entry is
never mirrored: it sticks at the old value, and whether the *next* transition on
that field is reported comes down to whether some unrelated delta happened to
launder the mirror in between. A check that says nothing about losing a state
therefore cannot reliably report gaining it a second time either. Every check
that can go both ways does, which is why an approval being dismissed and CI
catching up with the head are both reported.

Three checks stay one-directional on purpose, and each is exempted by name in
the tests with its reason:

| Check | Why it is not symmetric |
| --- | --- |
| `comment_count` | the mirror is a high-water mark. A deleted comment is not news; the price is that a comment posted after a deletion waits until the count passes the old peak |
| `head_sha`, `cla_ok` | a first observation is not a change, so a mirror still unset reports nothing |
| `ci_verdict`, `ci_failure_hint` | labels absent means the state is unknown, which is not news. The mirror keeps the last value the reader was told, so any later verdict still differs from it |

The tests read the mirrored field list off the receipt rather than a list of
their own, so a new `reported_*` field fails them until it says how it becomes a
delta.

## `runs.json`

```json
{
  "schema_version": 1,
  "channel_id": "<channel-id>",
  "completed_runs": 12,
  "streams": {
    "all": {
      "last_completed_run_utc": "2026-08-12T06:00:07Z",
      "last_completed_run_id": "manual-1786514497",
      "last_attempt_utc": "2026-08-12T07:00:02Z",
      "pending": null,
      "consecutive_failures": 0
    }
  }
}
```

`channel_id` is the owner record: a run serving a different channel stops and
writes nothing, which catches a prompt edited to point at another channel's
store. It does not catch the two-prompts-disagree case — a fresh empty store has
no owner to disagree with — which is why the report names the store it read.

The case it cannot catch at all is a store that is *absent* rather than wrong:
there is no owner record to read, because there is no file. `report` therefore
refuses to run against a store that does not exist, rather than adopting the
channel and reporting nothing tracked; `--allow-missing-store` overrides that,
and a run given it creates both the store and this record.

`audit` stops on the same absence, with the same diagnosis, and exits `2` — its
own code, so that "nothing was read" is never confused with either a clean store
or a finding. It has no override, because a clean audit of a file that was never
opened is not an output anyone wants; and it creates nothing at all, so an audit
pointed at a wrong path leaves neither store nor sidecars behind.

`pending` holds the uncommitted receipt: the run id, its cutoff, and the exact
`reported_*` values that committing will copy forward. If delivery fails, nothing
commits and the next run reports the same delta again. At-least-once, never
at-most-once: duplicated news is recoverable by a reader, lost news is not.

It is written the moment the report is rendered and cleared the moment the run
commits, so with `report --commit-after` it exists only for the few lines between
those two points — long enough that a run dying in the middle of writing its
report leaves the delta owed rather than consumed.

## `config.json`

```json
{
  "schema_version": 1,
  "repositories": {
    "owner/name": {"gitcode": "namespace/project"},
    "owner/other": {}
  },
  "watch": {
    "repos": ["owner/name"],
    "kinds": ["pull_request", "issue"],
    "state": "all",
    "window_days": 7,
    "authors": ["a-login", {"login": "another", "kinds": ["pull_request"]}]
  }
}
```

The only file here a person edits by hand, and the only one no command writes.
Two sections, because they answer different questions and are consulted by
different commands.

`repositories` is a **map keyed by the repository**, not a list, and the key is
its own capitalisation — the one place the owner's spelling survives, since an
identity key is lowercased. Its value holds that repository's properties:
`gitcode` pairs it with the project mirroring it on the second tracker, and an
empty value is a repository tracked on the first tracker alone. Every command
that resolves a pairing reads this, whether or not anybody is watched. Two
repositories sharing one project is refused rather than noted: each would be told
the other's merges and both reports would look right.

Under `watch`, `authors` is the sole required key; every other key is a default
that an entry may override for one person. **Authors and repositories are
separate axes and cross**: absent `watch.repos`, every author is watched in every
configured repository, and an author entry with its own `repos` scopes just that
person. An entry is a login or an object, so a bare list of names is a complete
section on day one and can gain per-person settings later without any existing
file being rewritten.

Keys starting with an underscore are ignored, which is where notes go. Unknown
keys are reported and ignored rather than refused, so a file written for a newer
script still tracks and watches what it names.

Absent is a no-op — a channel that configures nothing is a configuration.
Present and unparseable is a hard stop for every command, the report included:
watching nobody and failing to read whom to watch leave the store looking exactly
the same, and a report that lost its pairings looks exactly like one that never
had any.

For an older `<channel-id>.watch.json` file, rename it to
`<channel-id>.config.json` and nest its keys under `watch`. The old path is not
read.

`window_days` bounds **registration**, never reporting. It decides which closed
items enter the store; which rows the report names is decided by the watermark
and by nothing here.

## Lifecycle

```
active ──(terminal observed)──▶ terminal_pending ──(reported once)──▶ retired
   ▲                                                                    │
   └────────────────(reopened, or URL re-registered)────────────────────┘
```

Terminal is decided GitCode-first — a merged MR is **landed** — because the
GitHub PR is routinely closed for reasons unrelated to whether the change
landed. An item that vanishes from a tracker is `gone`, never merged; absence is
never terminal.

Each transition stamps its own date: `terminal_observed_utc` when the terminal
state was seen, `retired_at_utc` when the row retired, `ignored_at_utc` when
`track --unregister` took it out. A reopen clears the stamp it undoes rather
than leaving a date that reads as still true, so a stamp present is a state
currently held. `schema_version` on the row is the format it was last written
in.

**`lifecycle` is this skill's word about its own tracking and never the
tracker's word about the item.** `retired` covers a change that landed and one
that was abandoned equally, because both stop the reporting; `ignored` records
that a person took the item out of the report and says nothing whatever about
what the trackers think. To ask what became of an item, read the merge markers —
see *The three fields that look like the item's state*.

**Retired rows are kept, never deleted.** The row is the dedupe key and the
Slack message is still in the channel, so a deleted row would be re-registered,
re-reported, retired and deleted again forever. It is also the only way to tell
*reopened* from *newly tracked*, and the only record of how long an item took.

## Reading the store

`query` reads it and writes nothing: `--merged`, `--gh-state`, `--lifecycle`,
`--kind`, `--status` and `--repo` filter, and `--format count | table | json`
picks the shape. With no flags it prints a census of the whole store. The flags
are named after the fields above deliberately, so that the question a caller
asks and the document that answers it use one vocabulary.

**Prefer it to `grep` for every question about what is in the store**, and not
as a style preference. A `grep` is written against a schema the caller is
recalling rather than reading, and both ways of getting that wrong return the
same thing: a pattern naming a field that does not exist matches nothing, a path
that does not exist matches nothing, and each prints `0`. Neither the output nor,
on some hosts, the exit code distinguishes those from a true zero. `query`
refuses an absent store with its own exit code, and names the file it read and
how many rows were in it in every output, so a number always arrives with the
means to check it.

## Fixing the store by hand

One object per line with a stable `key`, so `grep`, edit, save. That covers
everything the CLI does not: a wrong classification, a wrong pairing, a bad title
rendering. Edit it with the file-write tool — `edit_file` for one line, or
`write_file` to replace the file — and not with a shell heredoc or a `>`
redirect: PR titles are arbitrary text, and one apostrophe or parenthesis in a
title ends the shell's quoting early and turns the rest of the row into shell
syntax, which fails identically every time the line is retried. A `>` redirect
would also truncate the store before the rewrite is composed, so a broken command
line loses every row rather than one. `track --unregister <url|key>` sets
`lifecycle: ignored`, which stops it being reported while keeping the dedupe key
so nothing can add it back.

Deleting the Slack message does **not** untrack: registration is a recorded fact,
and a deleted message would leave a row nothing can explain.

A line that does not parse is skipped for reading, kept verbatim at its original
position on write, and reported by line number. It is never silently dropped and
never rewritten.
