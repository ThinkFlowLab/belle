# Host integration checks

The tracker script keeps its own state and makes read-only tracker API calls. The
host supplies message triggers, channel history, scheduling, credentials, and
message delivery. Check these properties before installing the prompt templates
in `prompts.md`.

1. **Link trigger:** Check whether the trigger runs once per message or once per
   link. Check whether it sees edits, bot messages, and messages posted during
   downtime. Register every supported URL from one message in one `track` call.
2. **Message metadata:** Pass `--slack-ts` and `--slack-permalink` only when the
   host actually supplies them. Do not infer either from a clock or message text.
3. **Delivery receipt:** If the caller can observe a successful post, run `commit`
   after delivery. If the host posts the reply only after the turn ends, use
   `report --commit-after` for unattended runs and accept that a failed post can
   consume a report window.
4. **History:** Test a known nonempty channel before using history to reconcile
   missing links. Some hosts expose history to Slack turns but not to local CLI
   turns. An empty result alone does not prove the channel is empty.
5. **Storage and rendering:** Keep the store outside the installed skill directory
   and inside a backed-up, writable location. Ensure the renderer preserves the
   report's `<!-- belle:slack-thread-details -->` boundaries and table
   headings. If the host does not support those boundaries, deliver the report
   as one message and check that it stays within its message limits.

If the host provides an `auto_link` trigger with per-channel prompts, check
its behavior on the deployed version. The script's `--via auto_link` records
the trigger source. It does not create a trigger or install a schedule.

The script replaces the store's `.jsonl` suffix with `.run` to get its scratch
directory. Do not place scratch files inside the skill directory. A host may replace that directory when
updating the skill.
