# User guide

Everything about using `tenx` day to day: installing it, logging sessions,
custom metrics, multi-machine sync, and what happens when two machines
disagree. For architecture and how to add a feature, see
[EXTENDING.md](EXTENDING.md).

## Contents

- [Install](#install)
- [First-time setup](#first-time-setup)
- [The dashboard](#the-dashboard)
- [Logging a session](#logging-a-session)
- [Custom metrics](#custom-metrics)
- [Commands](#commands)
- [Multi-machine sync](#multi-machine-sync)
- [When two machines disagree](#when-two-machines-disagree)
- [How data is stored](#how-data-is-stored)
- [Exporting](#exporting)
- [Headless / scripted logging](#headless--scripted-logging)
- [Troubleshooting](#troubleshooting)

## Install

```bash
uv tool install git+https://github.com/<user>/tenx
# or
pipx install git+https://github.com/<user>/tenx
```

This installs the `tenx` command. Python 3.11+ is required; `textual` is the
only runtime dependency and is pulled in automatically.

## First-time setup

Your logged hours live in a **separate, private** git repository so the
`tenx` codebase itself can stay public. Point `tenx` at it:

```bash
tenx init --remote git@github.com:<user>/tenx-data.git
```

This single command:

1. Clones the remote into `~/.tenx` if it already has data, or creates a
   fresh git repo there if it's empty.
2. Writes `.gitattributes` with `sessions/*.jsonl merge=union` — the setting
   that makes two machines logging at the same time merge cleanly instead of
   producing conflict markers.
3. Creates `skills.json` if it doesn't exist yet.
4. Derives a `device_id` from your hostname (override with `--device-id`).
5. Creates an empty `sessions/<device_id>.jsonl` for this machine.

**Adding a second machine:** run the exact same command there. `tenx`
detects that a `sessions/<device_id>.jsonl` for that name is already in the
remote (written by the first machine) and refuses to proceed, telling you to
pass a different `--device-id`. Two machines writing to the same session file
is the one thing that isn't safe to merge automatically.

**No remote yet?** `tenx init` with no `--remote` still works — you get a
fully functional local-only setup (`⇅ local only` in the header) that you
can point at a remote later by adding one with `git remote add origin ...`
inside `~/.tenx`.

**Custom data directory:** set `TENX_HOME` to use a location other than
`~/.tenx`.

Once set up, just run:

```bash
tenx
```

## The dashboard

```
⇅ synced  ·  1,553.5h across 5 skills  ·  streak 5d (longest 20d)  ·  2026

  skill              hours    level   progress          streak   last 30d
  machine learning   614.0    10²     ███████░░░  57%   12d      ▂▅█▃▁▄█▂
  leetcode            88.5    10¹     ██████░░░░  65%   12d      ▁▃▂▅▂▁▃▁
  poker               75.4    10¹     ████░░░░░░  23%    3d      ▅▁▁▇▂▁▁▄

  Aug   Sep   Oct   Nov   Dec   Jan   Feb   Mar   Apr   May   Jun   Jul
  ░▓█░░▒▓█▒░░▓█▓▒░░░▒▓█▓▒░░▒▓██▓▒░░▒▓█▓▒░▒▓█▓░░▒▓█▓▒░░▒▓█▓▒▒░▓█▓▒░░▒▓█

> _
```

- **Header** — sync status, total hours, global streak, current heatmap
  year, and any warnings (unreadable lines, unresolved conflicts).
- **Skill table** — one row per non-archived skill, sorted by total minutes:
  cumulative hours, level (`10²` = you've logged over 100 hours), a progress
  bar to the next power of ten, current streak, and a 30-day sparkline.
- **Heatmap** — one column per week, one row per weekday, colored by how much
  you logged that day (5 levels: none, then four buckets from the quantiles
  of your own non-zero days — or fixed 30/60/120-minute cuts until you have
  30 days of history).
- **Command bar** — always focused. This is the entire input surface; there
  is no way to click into the table or heatmap, so you never lose your place
  mid-log.

## Logging a session

Just type. A bare line (no leading `:`) is a quick-add:

```
<skill> <duration> [date] [note...]
```

| you type | you get |
| --- | --- |
| `ml 1h30` | machine learning, 90 min, today |
| `ml 90` | bare number means minutes |
| `ml 90m` · `ml 1.5h` · `ml 1h30m` | all 90 min |
| `run 45m yesterday` | 45 min, yesterday |
| `lc 25m mon` | most recent Monday (today, if today *is* Monday) |
| `poker 2h 8/5 deep stack notes` | 5 Aug this year, with a note |
| `ml 1h 2026-08-05 grid search` | explicit ISO date |
| `ml 1h -2` | 2 days ago |

**Skill resolution**, in order: exact id match, then exact alias match, then
a unique case-insensitive prefix of any id, alias, or display name. `lc`
resolves to `leetcode` if that's the only skill starting with `lc`. An
ambiguous prefix lists the candidates; no match at all suggests `:new`.
Archived skills still resolve, so you can backfill without unarchiving.

**Durations** accept `N`, `Nm`, `Nh`, `N.Mh`, `NhMm`, `NhM` — up to 24 hours
per session, with `M` under 60 (`1h90` is rejected rather than silently read
as 150 minutes) and `1.5h30` rejected as ambiguous: is that 90 minutes plus
30, or 1.5 hours and 30 seconds?

**Dates** accept `today`, `yesterday`, a weekday name (`mon`…`sun` or spelled
out, meaning the most recent occurrence — today counts if today matches),
`M/D` (rolls back a year if that would otherwise be in the future), an
explicit `YYYY-MM-DD`, or `-N` for N days ago. Future dates are always
rejected.

**The date is only ever read from the third word**, and only if it parses as
one of the forms above. Everything else becomes the note. This is why
`ml 1h 5 papers read` keeps "5 papers read" intact instead of trying to
interpret `5` as a date — and why a note can't start with something that
looks like a duration/date typo and get silently misparsed; it's positional,
not fuzzy.

**`M/D` is only trusted as a date when it's the whole third word and nothing
follows it** — `lift 1h 12/8` logs to December 8, but `lift 1h 12/8 deadlift`
keeps "12/8 deadlift" as the note, since a real note is far more likely to
start with sets/reps notation (`3/4`, `5/3/1`, `12/8`) than a lone `M/D` is
to actually be a date sitting next to more note text. A malformed `M/D`-shaped
word (`13/45`) never kills the line either — it just falls back to note text
instead of erroring.

**Typos don't lose your input.** If a line doesn't parse, the text stays in
the box and an error appears below it — fix in place and press enter again:

```
> ml 1h3o sweep
✗ can't read "1h3o" as a duration - try 1h30
```

## Custom metrics

Every session has duration and date. Some skills want more — running wants a
distance, ML doesn't want anything extra — so each skill can declare its own
fields:

```
> :metric run distance
run now records distance - log it with "run 45m distance=..."

> run 45m distance=8.2 shoes=vaporfly easy pace
✓ running +45m · today · 12.4h total
```

- **Declaring is what makes a key a metric.** An undeclared `loss=0.23`
  stays in the note as plain text — that's what keeps a note like
  `todo: check a=b` from being misread as a metric assignment.
- Declared `key=value` tokens are pulled out of the line wherever they sit,
  so `run 45m distance=8.2 yesterday` still finds `yesterday` in what's left
  after the metric is removed.
- Values keep their type: things that parse as a number stay a number (so
  they can be summed and shown as a total in the detail view); anything else
  is stored as a text label.
- `:metric run -distance` stops recording a metric going forward. Sessions
  already logged with that value **keep it** — undeclaring only affects new
  entries, and the value still shows up in the detail view and CSV export.
- `:edit <n> distance=8.6` changes a metric on an existing session, the same
  way `:edit <n> 1h` changes a duration.

Metrics don't need to be visible anywhere to be useful — see
[Exporting](#exporting).

## Commands

Anything starting with `:` is a command (a colon in the middle of a note,
like `todo: rerun`, is just a colon — only a *leading* `:` triggers this).

Daily use:

| command | effect |
| --- | --- |
| `d <id>` | open the per-skill panel: a numbered list of recent sessions, streaks, and metric totals |
| `:rm <n\|id>` | tombstone a session — an append-only delete, not an in-place edit |
| `:edit <n\|id> <duration\|key=value>` | change a session's duration, or one of its custom metrics |
| `:undo` | tombstone the last session *this run of the app* added. The stack is in-memory only; after a restart it's empty and says so, rather than guessing and deleting something older |
| `:filter <id>` / `:filter off` | scope the heatmap to one skill, or back to all of them |

Occasional / admin:

| command | effect |
| --- | --- |
| `:new <id> [display name]` | create a skill. `id` is lowercase letters/digits/dashes and immutable; the display name can be anything and is editable later |
| `:rename <id> <new name>` | change the display name only — the id (and therefore every past session) is untouched |
| `:archive <id>` / `:unarchive <id>` | hide a skill from the dashboard table and the heatmap. Its all-time hour total is still counted; its own detail view still works |
| `:metric <skill> <key>` / `:metric <skill> -<key>` | declare or stop recording a custom metric |
| `:default <minutes>` / `:default off` | a bare skill name (`train`, no duration) logs this many minutes instead of erroring |
| `:year <YYYY>` | scroll the heatmap to a different year |
| `:sync` | force an immediate pull + push, instead of waiting for the debounce |
| `:conflicts` | list any collisions the sort key had to guess at (see below) |
| `:fix <n> <action>` | settle one of them |
| `:export csv [path]` | write every live session to a CSV file (defaults to `tenx-export.csv` in your data directory) |
| `:q` | quit, flushing any pending push first |

**`<n>` indexes the list you last looked at** — the numbered sessions from
`d <skill>`, or the numbered conflicts from `:conflicts` — so you rarely need
to type a session's full id. A partial id also works if it's unambiguous.

## Multi-machine sync

Sync runs on a background thread and never blocks logging:

- **On launch:** a `git pull --rebase --autostash` runs in the background.
  The dashboard renders from whatever is already on disk immediately, and
  refreshes when the pull lands.
- **On every log:** the line is appended to disk and the app moves on
  instantly — that append *is* the durability guarantee. A commit is queued
  for the background thread.
- **Push** is debounced (30 seconds of idle by default, configurable via
  `push_debounce_seconds` in `config.json`) and always forced through on
  `:q` or `:sync`.
- **Any git failure** — no network, bad credentials, an unreachable
  remote — is caught, logged to `~/.tenx/sync.log`, and shown as a status
  indicator. It never surfaces as a crash or a lost log line; your data is
  always safe locally first.

Status indicators in the header:

| indicator | meaning |
| --- | --- |
| `⇅ synced` | local and remote agree |
| `⇅ N ahead` | N commits queued to push |
| `⇅ offline` | the last network attempt failed; retried automatically |
| `⇅ local only` | no remote configured at all |

Two machines logging offline on the same day, or even the same session file
at the same second, merge cleanly on the next pull because
`sessions/*.jsonl merge=union` tells git to line-merge rather than
conflict-merge those files. You will not see `<<<<<<<` markers in a session
log from ordinary concurrent use.

## When two machines disagree

Last-writer-wins settles almost everything by timestamp, and that is **not**
a conflict: logging on your laptop and editing the same session on your
desktop an hour later is the ordinary flow, because the desktop had already
seen the value it was changing. A conflict is the much narrower case where
**the tie-break rule, rather than the clock, had to pick a winner** — and
there are exactly two situations where that happens:

| what happened | why it's a conflict |
| --- | --- |
| two machines wrote the same field **in the same second** | equal timestamps, so the winner came down to device name — arbitrary |
| a session deleted on one machine was **edited afterwards** on another | tombstones win by rule rather than by recency, so a strictly newer edit got discarded |

Nothing else is ever reported: different timestamps, one machine changing
its own mind, or two machines that happened to agree.

Conflicts are surfaced where you'll actually see them — appended to the echo
of whatever you just typed, and pinned in the header — and they **never**
block a log:

```
> ml 45m
✓ machine learning +45m · today · 2.5h total  ·  ⚠ 2 conflicts - :conflicts

> :conflicts
2 unresolved conflicts
  1  machine learning · 2026-08-09 · minutes
     kept  mac-studio    1h45        2020-01-01T11:00:00Z
     lost  laptop        2h          2020-01-01T11:00:00Z
     :fix 1 mine | theirs
  2  poker · 2026-08-09 · deleted on mac-studio,
     then edited on laptop at 2020-01-01T12:00:00Z
     :fix 2 keep | drop
```

Four fixed resolutions — no free-text merging, no prompts to design around.
There's no `newest`/`oldest`: a field collision is only ever reported when
both writes share the exact same timestamp (see the table above), so
"whichever happened later" has nothing to go on — it would just be picking
by device name, which isn't a real answer. `mine`/`theirs` is honest about
that: it asks you, not the clock.

| action | applies to | effect |
| --- | --- | --- |
| `mine` | a field | keep this machine's value |
| `theirs` | a field | keep the other machine's value |
| `keep` | a deletion | restore the session as a **new** record (tombstones are absorbing — the original id stays dead) |
| `drop` | a deletion | confirm the deletion; the newer edit is discarded |

`mine`/`theirs` only works from one of the two machines that disagreed —
resolve it there rather than from a third machine, which has no side to be
"mine."

A fix is just an ordinary `edit` or `del` written with the current
timestamp. Because it's clearly newer than everything the tie-break was
choosing between, there's nothing left to guess at — the conflict is
resolved everywhere on the next sync, including on a machine still running
an older build of `tenx`, since nothing about the log format changed.

## How data is stored

```
~/.tenx/
  skills.json              # whole-file, rewritten in place, rarely changes
  sessions/
    mac-studio.jsonl        # one file per device, append-only
    laptop.jsonl
  .gitattributes            # sessions/*.jsonl merge=union
  .gitignore                # config.json, sync.log
  config.json                # local only, not tracked: device_id, auto_sync, debounce
```

Each session log is one JSON object per line:

```jsonl
{"op":"add","id":"01J2X8QW3M...","skill":"ml","date":"2026-08-09","minutes":90,"note":"optimizer sweep","ts":"2026-08-09T14:02:11Z"}
{"op":"edit","id":"01J2X8QW3M...","minutes":105,"ts":"2026-08-09T14:04:52Z"}
{"op":"add","id":"01J2Y...","skill":"run","date":"2026-08-09","minutes":45,"extra":{"distance":8.2},"ts":"..."}
{"op":"del","id":"01J2X8QW3M...","ts":"2026-08-09T18:20:00Z"}
```

Nothing is ever rewritten — an edit is a new line, a delete is a tombstone
line. Loading means reading every device's file, sorting *all* operations by
`(timestamp, op kind, id, device)`, and folding them with last-writer-wins
per field. That ordering is what makes the result identical no matter which
order the files happen to be read in. A line that fails to parse is skipped,
counted, and reported (`⚠ N unreadable lines`) rather than silently dropped
or crashing the app.

Totals, streaks, levels, and heatmap buckets are always **derived**, never
stored — recomputed on every load. There's no cache to go stale and nothing
to migrate if the derivation logic changes.

**Backup and recovery:** the git history *is* the backup. Every append is a
commit; nothing is ever force-pushed or rewritten. `git log -p sessions/`
reconstructs any prior state.

## Exporting

`:export csv [path]` writes every live session as a flat file:

```csv
date,skill,minutes,note,id,distance,shoes
2026-08-08,run,62,tempo,01KZJY9BK0...,12.4,
2026-08-09,run,45,easy pace,01KZJY9BD...,8.2,vaporfly
2026-08-09,ml,135,attention ablation,01KZJY9BS...,,
```

Every custom metric — including ones you've since undeclared with
`:metric <skill> -<key>` — gets its own column, so nothing you've ever
recorded can be hidden by a later config change. A skill without a given
metric simply has a blank cell in that column. This is the intended way to
query metrics; there is no in-app query language (see
[EXTENDING.md](EXTENDING.md#ideas-for-extension) if you want to add one).

## Headless / scripted logging

For shell aliases, cron jobs, or anything else that shouldn't open the TUI:

```bash
tenx add "ml 1h30 optimizer sweep"
tenx add "run 45m distance=8.2"
```

Same grammar, same log file, same commit-and-debounced-push behavior as the
interactive app — just no screen.

## Troubleshooting

**"unknown skill" when I'm sure I typed it right.** Skill resolution needs an
*unambiguous* prefix. If you have both `poker` and `piano`, `p 2h` will list
both as candidates rather than guess.

**A skill I deleted from `skills.json` still has hours logged against it.**
By design — sessions reference a skill by id at *render* time, not at write
time. If the id is missing from `skills.json` (deleted config, restored from
an old backup, or a session from a machine with a newer `skills.json`), it
still renders in the table as `⟨that-id⟩` with its hours intact rather than
silently dropping them.

**The heatmap looks cut off in a small terminal.** A full year needs 57
columns (53 weeks + a 4-character label gutter). Below that, weeks are
clipped from the *left*, so the current week is always the one you keep.
This is intentional, not a rendering bug.

**`:undo` says there's nothing to undo, but I just logged something.** The
undo stack only covers adds made in the *current* run of the app. It's
intentionally not persisted — after a restart, `:undo` won't reach back and
delete something you can no longer see the context for. Use `d <skill>` and
`:rm <n>` instead.

**Two machines in different timezones, same calendar day.** The `date` field
is the literal local date you typed or the machine's local date at write
time — it's never recomputed from the timestamp. A session logged from
Singapore and one from New York on "the same day" (by each machine's own
clock) both land in that one calendar bucket, and relocating a machine
afterward never shifts a date already written.
