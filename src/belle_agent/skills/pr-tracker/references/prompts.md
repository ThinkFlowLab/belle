# Prompt templates

These are generic templates. Replace every `<skill>`, `<store>`, and
`<channel-id>` with an absolute installed skill path, one writable store path,
and the target Slack conversation ID. Use the **same store path** in registration,
watch, query, and report prompts. Keep the store outside the skill directory.
Do not put credentials in prompts. The script reads `GITHUB_TOKEN` from its
environment by default. Set that variable in the host's secret configuration.

Replace the store path's `.jsonl` suffix with `.config.json` to find the
configuration file. It holds repositories, GitCode pairings, and watched
authors. See `store-schema.md` for its schema. The script does not install
message triggers or create schedules.

## Link registration

Configure this on the host's link trigger for the target channel. If the host
supports per-channel prompts, use the channel identifier the host expects for
each override. Check the trigger's behavior on the deployed version.

```text
This channel tracks pull requests and issues. From the triggering message,
pass every GitHub pull-request or issue URL and every GitCode merge-request URL
in one command. Copy each URL exactly. Run:

python3 <skill>/scripts/pr_tracker.py track \
  --state-file "<store>" --channel <channel-id> \
  --via auto_link --render-table \
  --url "<URL>"

Repeat `--url` for each additional supported URL in the message. Run the command once. If it exits
nonzero, report the error. Otherwise, reply with the printed registration table
or the printed reason that a link was not tracked. Preserve the output; do not
fill in missing titles or states from another source. Do not write to either
tracker.
```

A host that provides the raw message body as stdin can instead use
`--message-file -`, avoiding model-selected URLs. Do not pass the prompt's own
links to that option. Pass `--slack-ts` or `--slack-permalink` only if the host
supplies those values directly.

## Author watch

Run this before a scheduled report, on a separate schedule. First configure
`watch.authors` and repositories in the configuration file. The first run
should use `--dry-run` to inspect the candidate count.

```text
Register the configured authors' recent pull requests and issues. Run:

python3 <skill>/scripts/pr_tracker.py watch \
  --state-file "<store>" --channel <channel-id>

This command only registers items. It does not refresh their state or advance
the report watermark. Report command errors. Do not change the watch list in
response to a routine scheduled run.
```

## Human-triggered report

Use this ordering when the caller can check message delivery and then run
`commit`. On a host that posts after the turn ends, the operator must run
`commit` after seeing the message. If nobody will do that, use the scheduled
variant.

```text
Produce the tracked-changes report. Run:

python3 <skill>/scripts/pr_tracker.py report \
  --state-file "<store>" --channel <channel-id>

If the command fails, report its error and stop. Send the report text exactly as
printed, including HTML comment boundaries, page headings, and empty-change
notices. Do not paraphrase, reformat, or omit sections. Do not commit inside
a turn that cannot check its own delivery. Give the printed `commit` command to
the operator by a separate route. Never commit before delivery. Do not write
to GitHub or GitCode.
```

Use `--full` when the user asks for a complete roster. The report writes the same
text to `report.md` in the `.run` directory beside the store. The script
replaces the store's `.jsonl` suffix with `.run` to get that directory. If a
language checker is available, check `report.md` before sending. This skill has
no checker dependency. Never redirect `report` output into the run directory.
The script clears that directory at the start of a run.

## Scheduled report

Use this variant only when the host posts the reply after the turn ends and the
caller cannot check its own delivery. With `--commit-after`, the script
advances the watermark when it renders the report. A failed post can consume
that window. An operator should monitor delivery.

```text
Produce the scheduled tracked-changes report. Run:

python3 <skill>/scripts/pr_tracker.py report \
  --state-file "<store>" --channel <channel-id> --commit-after

If the command fails, report its error and stop. Post stdout exactly as printed,
including HTML comment boundaries and page headings. Do not run `commit` again.
Do not write to GitHub or GitCode.
```

Add `--full` for a scheduled roster. Keep the watch schedule separate from this
report schedule. If the host can check delivery inside the turn, use the
human-triggered ordering instead.

## Store queries

For a question about counts, state, or tracked items, use the script rather than
parsing JSONL by hand:

```text
python3 <skill>/scripts/pr_tracker.py query \
  --state-file "<store>" --channel <channel-id>
```

Use `query --help` to select filters. A missing store, a failed refresh, or an
empty history scan is not evidence that no work exists.
