# 10^x hours

A terminal UI for logging deliberate practice across skills, with a
git-contribution-style heatmap for consistency and cumulative hour totals per
skill.

Manual entry only — no timers. Multi-machine sync over git, with append-only
logs that union-merge instead of conflicting.

```
  skill              hours    level   progress          streak   last 30d
  machine learning   614.0    10²     ███████░░░  57%   12d      ▂▅█▃▁▄█▂
  leetcode            88.5    10¹     ██████░░░░  65%   12d      ▁▃▂▅▂▁▃▁
  poker              310.2    10²     ████░░░░░░  23%    3d      ▅▁▁▇▂▁▁▄

  Aug   Sep   Oct   Nov   Dec   Jan   Feb   Mar   Apr   May   Jun   Jul
  ░▓█░░▒▓█▒░░▓█▓▒░░░▒▓█▓▒░░▒▓██▓▒░░▒▓█▓▒░▒▓█▓░░▒▓█▓▒░░▒▓█▓▒▒░▓█▓▒░░▒▓█
  ...

> ml 2h15 attention ablation
✓ machine learning +2h15 · today · 614.0h total
```

## Install

```bash
uv tool install git+https://github.com/<user>/tenx
# or
pipx install git+https://github.com/<user>/tenx
```

## Set up

Your hours live in a **separate, private** repo so this one can stay public.

```bash
tenx init --remote git@github.com:<user>/tenx-data.git
```

That clones the remote into `~/.tenx` (or creates it), writes the
`merge=union` `.gitattributes` that makes concurrent logging conflict-free,
and claims a device id derived from your hostname. On a second machine, run
the same command — if the device id is already taken, `init` stops and asks
you to pick another with `--device-id`.

Set `TENX_HOME` to point at a different data directory.

## Logging

Just type. A bare line is a quick-add:

```
<skill> <duration> [date] [note...]
```

| you type | you get |
| --- | --- |
| `ml 1h30` | machine learning, 90 min, today |
| `ml 90` | bare number means minutes |
| `ml 90m` · `ml 1.5h` · `ml 1h30m` | all 90 min |
| `run 45m yesterday` | 45 min, yesterday |
| `lc 25m mon` | most recent Monday |
| `poker 2h 8/5 deep stack notes` | 5 Aug, with a note |
| `ml 1h 2026-08-05 grid search` | explicit ISO date |

Skills resolve by exact id, then alias, then unique prefix — so `lc` finds
`leetcode`. Dates are only read from the third position, which is why
`ml 1h 5 papers read` keeps "5 papers read" as the note.

Durations accept `N`, `Nm`, `Nh`, `N.Mh`, `NhMm`, `NhM`, up to 24h per
session. Dates accept `today`, `yesterday`, `mon`–`sun`, `M/D`,
`YYYY-MM-DD`, and `-1`/`-2` for days ago. Future dates are rejected.

## Commands

A leading `:` means a command (a colon inside a note is just a colon).

| command | effect |
| --- | --- |
| `:new <id> [display name]` | create a skill |
| `:rename <id> <new name>` | change the display name only |
| `:archive <id>` / `:unarchive <id>` | hide from the dashboard and heatmap |
| `:rm <n\|id>` | tombstone a session |
| `:edit <n\|id> <duration>` | change a duration |
| `:undo` | tombstone this session's last add |
| `:filter <id>` / `:filter off` | scope the heatmap to one skill |
| `:year <YYYY>` | scroll the heatmap |
| `:detail <id>` (or `d <id>`) | per-skill view with numbered sessions |
| `:sync` | force pull + push now |
| `:export csv [path]` | flat dump of live records |
| `:q` | quit, flushing the push |

`<n>` indexes the list you last looked at, so you rarely type an id.

For scripting, there is a headless entry point that skips the TUI entirely:

```bash
tenx add "ml 1h30 optimizer sweep"
```

## How it stores things

`~/.tenx/sessions/<device>.jsonl` is an append-only op log — one JSON object
per line, one file per machine, always ending in a newline.

```jsonl
{"op":"add","id":"01J2X8QW3M...","skill":"ml","date":"2026-08-09","minutes":90,"ts":"..."}
{"op":"edit","id":"01J2X8QW3M...","minutes":105,"ts":"..."}
{"op":"del","id":"01J2X8QW3M...","ts":"..."}
```

Nothing is ever rewritten, so two machines logging offline on the same day
union-merge cleanly on the next pull — no conflict, no prompt. Loading sorts
every op by `(ts, op kind, id, device)` and folds them, last-writer-wins per
field, which makes the result identical no matter what order the files are
read in. Unreadable lines are skipped and counted, never dropped silently.

Totals, levels, streaks and heatmap buckets are always derived, never stored.
The git history is the backup: `git log -p sessions/` reconstructs any prior
state.

## Development

```bash
uv sync
uv run pytest
```

The engine (`models`, `ids`, `store`, `parse`, `stats`, `sync`, `config`)
imports no Textual and is tested headlessly; there is a test that enforces it.
