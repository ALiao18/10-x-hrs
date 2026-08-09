# 10^x hours

A terminal UI for logging deliberate practice across skills, with a
git-contribution-style heatmap for consistency and cumulative hour totals per
skill.

Manual entry only — no timers, no goals, no server. Multi-machine sync over
git, with append-only logs that union-merge instead of conflicting.

```
⇅ synced  ·  1,553.5h across 5 skills  ·  streak 5d (longest 20d)  ·  2026

  skill              hours    level   progress          streak   last 30d
  machine learning   614.0    10²     ███████░░░  57%   12d      ▂▅█▃▁▄█▂
  leetcode            88.5    10¹     ██████░░░░  65%   12d      ▁▃▂▅▂▁▃▁
  poker                75.4    10¹     ████░░░░░░  23%    3d      ▅▁▁▇▂▁▁▄

  Aug   Sep   Oct   Nov   Dec   Jan   Feb   Mar   Apr   May   Jun   Jul
  ░▓█░░▒▓█▒░░▓█▓▒░░░▒▓█▓▒░░▒▓██▓▒░░▒▓█▓▒░▒▓█▓░░▒▓█▓▒░░▒▓█▓▒▒░▓█▓▒░░▒▓█

> ml 2h15 attention ablation
✓ machine learning +2h15 · today · 614.0h total
```

## Install

```bash
uv tool install git+https://github.com/<user>/tenx
# or
pipx install git+https://github.com/<user>/tenx
```

## Quick start

```bash
tenx init --remote git@github.com:<user>/tenx-data.git   # or omit --remote for local-only
tenx
```

Then just type: `<skill> <duration> [date] [note...]`, e.g. `ml 1h30 optimizer sweep`.
A leading `:` is a command — `:new`, `:rm`, `:detail`, `:sync`, and a dozen
others. Full grammar, every command, custom metrics, and multi-machine
conflict handling are in the **[user guide](docs/USER_GUIDE.md)**.

## Documentation

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** — installing, logging,
  commands, custom metrics, multi-machine sync, what happens when two
  machines disagree, data format, exporting, troubleshooting.
- **[docs/EXTENDING.md](docs/EXTENDING.md)** — architecture, the engine/UI
  boundary and why it's enforced by a test, worked examples for adding a
  command or a grammar element, how to extend the op-log format without
  breaking older machines, testing conventions, and a running list of
  ideas that are deliberately not built yet.

## Development

```bash
uv sync
uv run pytest
```

The engine (`models`, `ids`, `store`, `parse`, `stats`, `sync`, `config`)
imports no Textual and is tested headlessly — enforced by
`tests/test_engine_isolation.py`, not just by convention.
