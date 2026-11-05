# Shape of the delivered report

The script renders this; nothing here asks a model to assemble it by hand. It is
written down because the shape encodes decisions that are easy to undo by
accident.

## Sections

```
```blockkit
{"blocks": [{"type": "context", ...}, {"type": "context", ...}]}
```                     ← when this ran, what it found -- see *The brief* below
<the per-repository carousel, if there is one to show>
<!-- belle:slack-thread-details -->
*Newly tracked*        ← registrations since the last delivered report
*Changed*              ← the delta, ranked by impact
*Still waiting*        ← re-surfaced items, compact
*Closed and landed*    ← rotation, once per item
<!-- belle:slack-thread-details -->
*Roster*               ← only with --full; one marker per extra page
<!-- belle:slack-thread-details -->
*Detail*
*Coverage and gaps*
*Source and registration*
```

Every section is omitted when empty, and so is a marker with nothing under it —
what is above is the full shape, not a fixed sequence. Each marker is a message
boundary for hosts that recognise it: the brief goes to the channel, and everything after each
marker becomes a reply in that message's thread. A host that recognises no
marker renders them all as HTML comments and the report reads as one message; a
host that splits on the first marker only delivers the brief and one reply
holding the same sections in the same order. Both are graceful degradations and
neither is a failure to work around.

**Four messages at most, and the order is the point.** The tables are what a
reader opens the thread for, so they arrive first; the roster, which is the
standing list rather than the news, follows them; the prose that explains all of
it is reference — read once, skipped thereafter — so it comes last.

The splits are not cosmetic. Slack's Block Kit limits are counted per message,
so each boundary is another budget: prose that could never render as a table
spends none of the tables', and the roster spends none of the deltas'. That
second point is the one that binds, because the roster is the only section whose
length nobody chose — see *Tables* below.

**A run without a roster stays at three messages, and a quiet one at two.** The
report writes a boundary only where a message follows it: with no `--full` the
piece between the second and third markers is empty, so no marker is written
there, and on a run where every table section is empty only the boundary between
the brief and the prose survives. The boundary costs nothing on the runs that do
not need it.

This is the report's own doing, not the connector's. A host may well drop an
empty piece — check the host behavior — but relying on that would make the report
correct on one connector and a source of blank replies on the next, silently,
and the report is the only thing that knows which of its sections are empty.

## The brief

Two `context` blocks, and they are the whole of what a reader skimming the
channel sees: when this ran, and what it found.

```blockkit
{
  "blocks": [
    {
      "type": "context",
      "elements": [
        {"type": "mrkdwn", "text": ":clock3: 2026-08-14 12:00 · 12 tracked (since 2026-08-14)"}
      ]
    },
    {
      "type": "context",
      "elements": [
        {"type": "mrkdwn", "text": ":arrows_counterclockwise: 1 newly tracked · 3 changed · 1 closed and landed"}
      ]
    }
  ]
}
```

Two blocks, not one, because the two kinds of fact — when this ran, what it
found — read better kept apart than run together on one line. Each block holds
exactly one text element rather than one element per fact: a `context` block's
ten-element ceiling counts a bare `" · "` the same as a fact, so writing five
short facts as nine elements (five facts, four separators) spends nearly half
the budget on punctuation for nothing. The separators live inside one string
instead, which spends one element on any number of facts.

A run with no items in any section prints `No changes` in the second block.

The tally counts sections in **section order**, not impact order, so it reads
top to bottom against the tables in the thread below it. It names counts and not items: naming the
sharpest item reads better on a one-item run and turns into an arbitrary choice
of headline on a twenty-item one, and the item is one click away either way.

The roster's count belongs in the second block even though the roster is a
message of its own. Which message a section is delivered in is transport, not
reading order: the thread still reads top to bottom in the order the tally
names, and leaving the roster out would have a `--full` run announce less than
it delivers.

Neither block says "details in thread". Slack draws its own reply count, and on
a host that does not recognise the marker the sentence would be false.

## The first context block

`<run timestamp> · <N> tracked` and, where one was given, `repo: <filter>`,
followed by `(since <date>)`. The run timestamp carries both a date and a time
— a daily job needs the date, a frequent one needs the time — while `since`
carries a date only: it anchors how far back the tracked set reaches, and a
time of day on top of that date was printed once and was never useful. All of
it qualifies the second block directly — "3 changed" is unreadable without the
size of what is being watched and the window the delta reaches back over, and a
filter changes what is being counted — so all of it is summary rather than
provenance.

Nothing else stays. Which store was read and how an item gets into it are both
true and useless to a skimmer, and between them they were most of the old
line's length. They are in *Source and registration* below, which is where a
reader diagnosing a wrong report already is.

## Source and registration

Named for the two questions it answers — where the rows came from, and how one
gets in — rather than for the fields it prints, so that the store line and the
registration line sit under one heading instead of two.

It is last because a correct report never needs it. On the run where the report
is wrong it is the first thing to read, and it is one click away either way.

The store line is the only defence against the two prompts naming different
stores. That failure is silent by construction — registration writes one file and
the report reads another, permanently empty one — so the section names the store
that was actually read and how many rows were in it. A store name nobody
recognises, or `(0 rows)` under a channel that has been collecting links all
week, is then legible as a misconfiguration instead of as "nothing happened".

The store is **named, not located**: the file name only. A store path is input —
the prompt supplies it because the script cannot find the store without it — and
it is not something a reader can act on. Emitting it published a host's username
and directory layout to the channel on every run, and it was long enough that the
model relaying the report mistyped it, which made a corrupted path
indistinguishable from a misconfigured one. The file name keeps the whole signal:
a store nobody recognises still reads as a wrong name, and a store the
registering prompt never writes to still reads as an implausible `(0 rows)`.

The registration line names **the routes this store actually shows**, read off
the rows rather than off the configuration. A store with only posted links says
`Registration: link trigger only`: registration reacts to messages as they arrive
and nothing replays them, so a quiet report is not evidence of a quiet week. A
store the author watch has written to says so as well, and once both routes are
configured, a report naming only one of them is a route that has stopped running
rather than a quiet week — which is the whole reason the line is derived from the
store instead of being a fixed sentence.

The two sections divide the point cleanly: this one states the mechanism, and
*Coverage and gaps* states the consequence in the reader's terms — that anything
posted while the trigger was down is missing until it is posted again, and that
the watch covers the people on its list and nobody else.

**The route is also on each *Newly tracked* row, and on no other row.** "Twelve
newly tracked" reads completely differently depending on whether twelve people
asked about something or one sweep swept: those are different intents, and a
reader deciding whether to open the thread is entitled to the difference. It
would have been simpler to merge them and let a registration be a registration —
but the merge is the lossy direction, and it makes the report quietly noisier
than anyone chose. It is news exactly once: by the time an item reaches
*Changed*, how it arrived is settled history and a route column would be the same
value repeated every run. The route is rendered in the reader's words, never in
the store's vocabulary, which is a value the store keeps and not one a channel
should have to learn.

The run id and channel are here too. They are provenance by the same definition
and were the last line of *Coverage and gaps* only because there was nowhere
better; *Coverage and gaps* now holds gaps and nothing else.

## Delta only

An item is named if and only if one of its `reported_*` mirrors disagrees with
the current fact. Not a window, not "the last 24 hours": the mirror is what the
reader was last told, however long ago that was, so a run after two days down
covers two days without a special case.

**Impact ranking**, highest first:

    landed → conflicted state changed → CI went red → approval gained or
    dismissed → lgtm rose → CLA state changed → stale_ci changed →
    CI went green → new maintainer comment → author push

A rank names a field, not a direction: gaining an approval and losing one rank
alike, because a reader told "approved" and never told otherwise goes on
believing it.

## Tables

Tables are emitted as Markdown, with no marker of any kind, and every item
section is a table at any size — one row included. An earlier threshold rendered
fewer than four changes as bullets; most real reports are below it, so the
bullet form was what a reader actually saw, and it put the deltas last, after a
title long enough to push them off the line.

No table is ever in the channel message. The delta tables sit between the first
and second markers and arrive as the first reply; the roster sits between the
second and third and arrives as its own.

The columns are `Item`, `Author`, `What changed`, `Status`, `Title`, `See also`,
and every table that renders rows renders all six — the roster included.
`Author` is second because it identifies the row with the item rather than
describing what happened to it; the change columns that follow are the news. It
prints the login the tracker gives, `[bot]` suffix and all, so an app account
stays distinguishable from a user of the same name and automation is legible on
sight. An item with no author prints `—`, which no login can be: the rows
without one are the links that are neither pull request nor issue and have no
author to carry.

**The column is not free.** Measured on real rows it costs roughly 15% of the
per-table ceiling, and it costs it on every table at once. Six columns are
nowhere near Slack's limit of 20 — characters are what bind, and a column is
paid for on every row.

`Item` names the item on one tracker only. Where an item has a paired
identifier on another tracker the store keeps it and the table no longer prints
it: the cell's link has one destination, and the second code was a lookup key
for a system the reader is not in. It sat on half the rows and did not change
between runs, which is this report's own test for a column of noise. What the
pairing *means* survives without it — a change that lands through the other
tracker reads `landed` in `Status` and says so in `What changed`, which is the
part anyone acts on — so this is a rendering decision and not a loss of
information. `audit` still shows the pairing.

**Confirm the host renders Markdown tables before adopting this.** Native table
rendering is usually something a host has to have turned on; where it is off, a
table reaches the channel as raw pipe characters. Slack's Block Kit limits bound
what can render at all — 100 rows, 20 columns, 50 blocks and 10 000 table
characters — and apply on any host rendering through Block Kit.

Two of those bite here, and they bite at different scopes:

- The character and block budgets are **per message**. Every table in a message
  shares them, so sections that each fit on their own can still exceed them
  together.
- The row limit is **per table**: more than 100 rows including the header — 99
  report rows — cannot render at all, and no amount of splitting the message
  helps.

Breaching either does not truncate the table: the renderer declines the whole
message and it is delivered as text, which on Slack means every table in it
arrives as raw pipe characters. The content is complete and the formatting is
gone.

The markers are what keep the per-message budgets from being one budget, and
where they go follows from which sections can grow. The delta tables are as long
as the news, which is usually short. The roster is as long as the roster: it is
the only section whose size nobody chose, it grows with every item registered
and never shrinks until items retire, and it is therefore the section that
reaches a limit first. Giving it its own boundary is what stops its growth from
being charged to the deltas.

The measured difference, against real rows: the delta tables alone and the
roster alone each rendered at a size that, put together in one message, ran out
of table characters at **under half** of it — the breach taking the deltas'
formatting down with the roster's, for no reason except that they shared a
message. With the roster split off, each is bound by its own budget again and
the combined ceiling is the smaller of the two rather than roughly half of it.

Three things follow that are worth stating plainly:

- **The row cap is usually not what binds.** 99 report rows is the ceiling only
  while rows are narrow enough that 10 000 table characters last that long, and
  with six columns of real data they do not. The character budget is reached
  first, and the delta tables reach it before the roster because they carry a
  column of change text the roster leaves empty. Never quote 99; measure.
- **Splitting the message cannot lift the row cap, and does not lift the
  character cap either — it stops the two tables sharing one.** Beyond a single
  table's own budget the only remedy is fewer or narrower rows: a filter,
  shorter titles, or paging the roster across messages.
- **Every column is paid for on every row.** Slack allows 20 and the report uses
  6, so the column count is nowhere near binding — but each one added costs a
  slice of the ceiling of every table at once.

## Paging the roster

The roster is paged so that its growth cannot cost it its formatting. The
renderer writes a marker between pages, so each page is a message and each
message is a budget; the same mechanism the rest of the report already uses, and
nothing is added to the host for it.

**The break is on accumulated width, never a row count.** Characters are what
bind, so sixty short titles fit where sixty long ones do not, and any fixed
count is either wrong for the wide case or wasteful for the narrow one. A page
closes when the next row would take it past the budget. The quantity accumulated
is the one the host totals when it decides whether to render — the visible text
of every cell, header row included, link destinations excluded — computed from
the very cells the renderer will print, so an estimate cannot drift away from
what is actually emitted.

The row cap is enforced beside it, because the two limits are on different axes.
Narrow rows reach 99 rows with characters to spare; wide rows reach the
character budget while still short of 99. Neither check sees the other coming,
so both are made.

Pages are numbered — `*Roster (2/3)*` — which is what makes a missing page
visible: a `2/3` arriving without a `3/3` reads as a failed delivery rather than
as a shorter roster. **A single-page roster is titled `*Roster*` and not
`(1/1)`**, because numbering a list of one tells a reader to go looking for a
message that does not exist.

The pages are held a little under the host's own budget. The accounting here is
a model of the host's, and a model that is exact today is one release away from
being wrong by a little; a page that breaches loses its formatting entirely,
while a page that stops a row early costs a row. The margin makes the failure
mode "one more page".

**Past a few pages the roster is stated rather than listed.** Paging further is
mechanically fine and useless: a list that long is not one a reader scans in a
thread, and the messages it takes push everything after it out of reach. Over
the limit the section prints the count and how to narrow the run, and no table.
The count is always given, so nothing is ever silently dropped — what is
withheld is a list nobody could read.

Message counts, then, at any size: a quiet run is two, an ordinary run three, a
`--full` run whose roster fits one page is four, and a paged one is three plus
the number of pages.

**Where the report passes through something that reads it and posts it on, that
relay is the limit that binds first — well before any of the numbers above.**
The page headings and the boundaries between them are the paging; a relay that
reproduces every row but tidies one heading away merges two pages into a single
table too large to render, and the message arrives as raw pipe characters. That
is not hypothetical, it has been observed, and it is why the delivery
instructions insist on copying the report out whole rather than reassembling it.
A roster large enough to need paging is also large enough that a relay starts
editing it, so the practical ceiling is lower than the rendering one and is set
by whatever is carrying the report.

A report-format convention that forbids Markdown tables outright generally
predates native table rendering rather than describing what a host can do now. If
one is in force, check what the host actually renders before either obeying or
departing from it.

## The three edge cases

- **Nothing changed** — post the second context block anyway, reading `No
  changes`. A silent run is indistinguishable from a broken schedule, an
  expired token or an empty roster, and reading a stopped skill as a quiet one
  is the failure this design most wants to avoid.
- **Everything changed** — cap the narrative, put the remainder in a table, and
  say what was left out and why. Never truncate silently.
- **Tracker unreachable** — advance nothing, retire nothing. Name the items that
  did refresh and say plainly how many did not. Cached state is never presented
  as current.

## CI lines

A red build is always surfaced, whatever its cause: a flake means retrigger or
raise it with the reviewers, a real failure means rework. Both are work. The
classifier only says *which kind*:

> `#2455` — **CI failed** · matches a known-flake signature (here the one named
> "example.com unclosed socket") → retrigger, or raise with maintainers

> `#2718` — **CI failed** · 2 failing tests, no known-flake signature → likely
> needs rework

> `#2720` — **CI failed** · could not classify (report unavailable)

`unclassified` is printed as unclassified. Calling an unfetchable artifact a
flake is the one outcome worse than saying nothing.

The classifier is only as honest as its signature file, and losing that file is
invisible from the report: "no known-flake signature" is what an unmatched failure
says *and* what a run holding no signatures at all would say about every red
build. So a run never quietly has none. A signature path that was named and not
found stops the run; a signature file that exists and cannot be read stops it too;
a set that can classify nothing is stated on stderr.

## `mergeStateStatus`

Where the merge gate is not GitHub's own, it reads `BLOCKED` on every open PR
forever, so it appears once, as a footnote in *Detail*, and never in a row.

## Language

Titles arrive in whatever language they were written in, and a large share of the
GitCode side is Chinese. `title_source` is kept verbatim for diagnosis;
`title_rendered` is what the reader sees, cached once per item. A title that
still needs rendering is listed in *Detail* with its source text, and the finished
text can be checked by a host-side report-language checker, if one is
available. This skill ships no checker. A failed check is fixed by rendering the
title, never by dropping the item.
