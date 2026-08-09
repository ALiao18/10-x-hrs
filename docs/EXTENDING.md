# Extension guide

How the codebase is put together, and how to add to it without breaking the
guarantees that make multi-machine sync safe. For day-to-day usage, see
[USER_GUIDE.md](USER_GUIDE.md).

## Contents

- [Architecture at a glance](#architecture-at-a-glance)
- [The one rule that matters most: engine vs. UI](#the-one-rule-that-matters-most-engine-vs-ui)
- [The data model](#the-data-model)
- [Worked example: adding a colon command](#worked-example-adding-a-colon-command)
- [Worked example: extending the quick-add grammar](#worked-example-extending-the-quick-add-grammar)
- [Extending the op-log format safely](#extending-the-op-log-format-safely)
- [Collision detection is automatic — mostly](#collision-detection-is-automatic--mostly)
- [Adding a widget](#adding-a-widget)
- [Testing conventions](#testing-conventions)
- [Project philosophy](#project-philosophy)
- [Ideas for extension](#ideas-for-extension)

## Architecture at a glance

```
src/tenx/
  models.py     dataclasses: Skill, Session, Op, Collision, Party
  ids.py        ULID generation (stdlib only)
  store.py      read/append jsonl, replay + fold ops, load/save skills.json
  parse.py      quick-add grammar + colon-command grammar (pure function)
  stats.py      totals, levels, streaks, heatmap buckets, sparklines
  config.py     ~/.tenx paths, device_id, tenx init
  sync.py       git subprocess wrapper, background worker thread
  app.py        Textual App: layout, command dispatch, sync-thread bridge
  widgets/      CommandBar, SkillTable, Heatmap, DetailPanel
  __main__.py   CLI: tenx / tenx init / tenx add
```

Data flows one way on read: `store.load()` → `stats.aggregate()` →
widgets. Data flows one way on write: a colon command or quick-add builds an
`Op` → `store.append_op()` writes it → `app.reload()` re-reads and
re-aggregates everything. There is no in-memory mutation of session state;
every write is followed by a full reload from disk. That's slower than
patching a dict, but it means the in-memory view can never drift from what's
actually on disk, and there is nowhere for a stale-cache bug to hide even
across a background pull landing mid-session.

## The one rule that matters most: engine vs. UI

**`models`, `ids`, `store`, `parse`, `stats`, `sync`, and `config` contain no
Textual imports.** This is enforced by
`tests/test_engine_isolation.py`, which both greps the source for `textual`
and imports the whole engine in a subprocess with `textual` import-blocked.

Why this matters more than it looks: it's what makes the headless `tenx add`
possible, what lets `parse.py` be fuzzed with no filesystem or event loop
present, and what makes every engine test run in milliseconds instead of
booting a TUI. If you're adding logic that *could* live in either an engine
module or `app.py`, it belongs in the engine module — `app.py` should be
thin wiring (parse the result, call an engine function, update a widget),
not where decisions get made.

If you genuinely need to touch this boundary — a new engine module, a
Textual import creeping into `stats.py` because a formatting helper felt
convenient there — treat that as a design decision worth pausing on, not a
one-line change to wave through.

## The data model

- **`Skill`** (`models.py`) — `id` (immutable, `[a-z0-9-]+`), `name`
  (editable), `created`, `archived`, `aliases`, `metrics`. Whole-file
  read/write via `store.load_skills` / `store.save_skills`; conflicts here
  are rare enough that they're resolved by hand rather than merged.
- **`Op`** (`models.py`) — one line of a session log. `fields` holds only the
  payload keys actually present on that line (so an `edit` can touch one
  field without clobbering the rest). `device` comes from the *filename*,
  never from the line itself.
- **`Session`** (`models.py`) — the materialized, live record `store.replay`
  produces by folding every `Op` for one id. This is what the rest of the
  app reads; nothing downstream of `replay` ever looks at raw `Op`s.
- **`Collision` / `Party`** (`models.py`) — what `store.replay` emits when
  the sort-key tie-break, not a timestamp, decided a field's value. See
  [Collision detection is automatic](#collision-detection-is-automatic--mostly).

### Replay and ordering

`store.replay` sorts every `Op` from every device by
`(ts, op_rank, id, device)` — `op_rank` is `add < edit < del`, which exists
because `ts` alone can't order an add and a delete written in the same
second (they can share both an id and a device). It then folds them with
**per-field** last-writer-wins: an `edit` only overwrites the fields it
actually names. A `del` is a hard tombstone; nothing after it revives the
record except an explicit later write from *any* device recognized as a fix
(see collisions, below).

This ordering rule is one of the three things the original build spec asks
you to raise before changing, alongside the union-merge strategy and the
engine/UI split — it's load-bearing for every multi-machine guarantee in the
app.

## Worked example: adding a colon command

Say you want `:dup <n>` — duplicate a session logged today, so you can
quickly re-log a recurring block. Here's the whole shape of adding a
command, using that as a concrete (unshipped) example.

**1. Register it in `parse.py`'s `COMMANDS` table:**

```python
COMMANDS: dict[str, tuple[int, int | None, str]] = {
    ...
    "dup": (1, 1, ":dup <n|id>"),
}
```

The tuple is `(min_args, max_args_or_None, usage_string)`. `parse_command`
already validates arity and unknown-command names against this table — you
don't write that validation yourself. If the command needs its own
value-level validation (like `:fix`'s action must be one of six words, or
`:year`'s argument must be four digits), add a branch next to the existing
ones in `parse_command`, following the pattern already there for `year`,
`export`, `fix`, and `metric`.

**2. Add the handler in `app.py`:**

```python
def _cmd_dup(self, command: Command) -> None:
    session = self._find_session(command.args[0])
    if session is None:
        return
    op = store.make_add(session.skill, self.today, session.minutes, session.note)
    self._append(op)
    self.bar.ok(f"duplicated as {self._display_name(session.skill)} · today")
```

`app._command` dispatches `:dup` to `_cmd_dup` automatically —
`_command` looks up `_cmd_{name}` by convention, so naming the method
correctly is the entire wiring step. `self._find_session` already gives you
`<n>`-or-id resolution against the last-displayed list, and `self._append`
already handles writing the op, reloading state, and queuing the sync commit
— reuse it rather than calling `store.append_op` directly.

**3. Test it.** Add a parse-level test (`tests/test_parse.py`) asserting
`:dup 1` parses to the right `Command`, plus arity/error cases. Add a pilot
test (`tests/test_app.py`) that logs a session, opens `d <skill>`, runs
`:dup 1`, and asserts the duplicate landed on disk. See
[Testing conventions](#testing-conventions).

That's the whole pattern: **grammar** (parse.py) → **effect** (app.py
`_cmd_*` method) → **test at both layers**. Every existing command follows
this shape; skim `_cmd_rm`, `_cmd_edit`, or `_cmd_metric` in `app.py` for
more variations (metric-vs-duration disambiguation in `_cmd_edit`, the
two-step resolve-then-mutate pattern in `_cmd_archive` /
`_change_skill`).

## Worked example: extending the quick-add grammar

The quick-add line (`<skill> <duration> [date] [note...]`) is parsed by
`parse._parse_add`, which is a pure function: `(text, skills, today) →
QuickAdd | ParseError`. No filesystem, no clock read (unless `today` is
omitted), no randomness — which is what makes it safe to fuzz and to unit
test exhaustively with hand-picked dates.

Custom metrics are the precedent for "add a new token type to the grammar."
The shape to follow:

1. **A recognizer** (`parse_metric` / `_METRIC_TOKEN`) that decides whether
   a token belongs to your new grammar element, returning `None` if not —
   never raising, never consuming a token it isn't sure about. This is what
   keeps `ml 1h todo: check a=b` from being misparsed: the metric recognizer
   requires the key to be *declared* on that skill, so an incidental `=` in
   a note is never ambiguous.
2. **Extraction before positional parsing.** Metrics are pulled out of
   `tokens[2:]` *before* the date-position rule runs (`_take_metrics` is
   called, then `parse_date` looks at what's left). If you add another
   token type, decide deliberately where it sits relative to the existing
   date-is-only-position-3 rule — that rule is a documented, tested
   invariant (`ml 1h 5 papers read` must never eat "5" as a date), and it's
   easy to break by extracting your new tokens after date-parsing instead
   of before.
3. **Whatever's left becomes the note**, unconditionally.

If your addition needs an extra field on `QuickAdd` (metrics needed one:
`extra`), keep it a `tuple[tuple[str, Any], ...]` or similarly hashable
shape rather than a `dict`, so `QuickAdd` instances stay comparable —
`test_parse_is_pure` asserts `parse(x) == parse(x)`, and a `dict` field
would break that.

## Extending the op-log format safely

This is the one change class the build spec says to *raise before making*,
not just make — session logs are the multi-machine contract, and every
device, including ones running an older build, has to keep working.

**The rules, as followed by custom metrics (the one precedent so far):**

- **Additive only.** New data goes in a new key. Custom metrics didn't add
  new top-level keys (`skill`, `date`, `minutes`, `note`, `ts`, `op`, `id`)
  at all — they ride in a nested `"extra": {...}` object specifically so
  they can never collide with a future top-level field name.
- **Old readers must degrade gracefully, not crash.** A build that predates
  `extra` simply doesn't look for that key and sees a session with no
  metrics — not a parse error. `Op.from_dict` already ignores unrecognized
  keys by construction (it reads a fixed field allowlist); make sure
  anything you add follows the same pattern rather than doing `**payload`
  passthrough.
- **Unknown `op` values are skipped, not errors** (see `store.replay`'s
  `else: skipped += 1` branch) — this is what lets a future op kind roll out
  without breaking machines that haven't updated yet. If you're tempted to
  add a new `op` kind rather than a new field, make sure it degrades the
  same way: an old reader should skip the line's *effect*, not the whole
  file, and definitely not corrupt the merge.
- **In-memory, flatten to a prefixed key** so the new fields ride the
  *existing* per-field last-writer-wins and collision machinery for free
  instead of needing their own. Metrics flatten `extra.distance` to
  `extra:distance` in `Op.fields`/`Session.extra` at the read/write
  boundary (`models.py`'s `EXTRA_PREFIX`), so `store._Record.merge` and the
  collision detector in `store.replay` never had to change — they just see
  another field name. Prefer this over writing a parallel merge path.
- **A resolution is always just a normal, later-timestamped write** — never
  a new flag or op kind meaning "this is a fix." An early draft of conflict
  resolution added a `"fix": true` marker to settled ops; it was removed in
  favor of "a write timestamped later than everything the tie-break
  compared settles the field," specifically because that requires no format
  change and old builds apply the resolution correctly without knowing
  anything special happened.

## Collision detection is automatic — mostly

Because collision detection (`store.replay`'s `note_clash`) walks whatever
fields a merge actually displaced, **a new scalar field you add via the
`extra:`-prefix pattern above gets collision detection for free** — no code
change needed in `store.py`. Two machines disagreeing on a new metric in the
same second will show up in `:conflicts` exactly like two machines
disagreeing on `minutes`.

What is *not* automatic: if you add a new **op kind** (rather than a new
field on an existing op), you need to decide explicitly how it interacts
with `add`/`edit`/`del` in the ordering and folding logic, and whether a
"deleted, then written elsewhere" style collision applies to it. Read
`store.replay` end to end before doing this — the delete/tombstone handling
in particular (the `dead` dict, `graveyard`, and the `deleted`-kind
`Collision`) is the part most likely to need a matching case for a new op.

## Adding a widget

Widgets live in `src/tenx/widgets/` and are wired into `app.py`'s
`compose()`. Two things every widget in this app does, on purpose:

- **`can_focus = False`.** The `Input` in `CommandBar` is the *only*
  focusable widget in the whole tree. This isn't enforced by a convention
  you have to remember to apply defensively — it's structural: nothing else
  in the layout is focusable, so focus cannot leave the entry box no matter
  what a user presses. If you add a new interactive-looking widget (a list
  you might want arrow-key navigation on, say), you're making a deliberate
  choice to break that invariant, and `tests/test_app.py`'s
  `test_the_command_bar_is_the_only_focusable_widget` will catch it if you
  don't mean to.
- **`update_*(...)` methods, not reactive bindings to `app` state.**
  `SkillTable.update_rows`, `Heatmap.update_data`, `DetailPanel.update_data`
  /`update_lines` are called explicitly from `app.refresh_views()`. Every
  mutating command calls `self.reload()` (or `refresh_views()` directly for
  view-only changes like `:filter`/`:year`), which pushes fresh data into
  every visible widget synchronously, inside `batch_update()`. This is what
  makes the table and the heatmap's "today" cell update in the same
  render frame as a quick-add — see `on_input_submitted` in `app.py`.

## Testing conventions

```bash
uv sync
uv run pytest          # full suite
uv run black --check --line-length=110 src/ tests/
uv run flake8 --max-line-length=120 --extend-ignore=E203 src/ tests/
```

- **`tests/test_parse.py`** — table-driven, pure-function tests. No
  filesystem, no `tmp_path`. `today` is always passed explicitly.
- **`tests/test_store.py`** — round-trip, replay-ordering, and corruption
  resilience (malformed lines, missing trailing newlines, concurrent
  appends from two real subprocesses). Grounds the multi-machine guarantees
  at the file level, without git.
- **`tests/test_sync.py`** — integration tests using real bare git repos and
  real clones in `tmp_path`, asserting **zero conflict markers** after
  concurrent pushes/pulls. This is the test class to extend if you touch
  `sync.py` or `config.py`.
- **`tests/test_app.py`** — Textual pilot tests. There's no `pytest-asyncio`
  dependency; each test wraps an async body with a small `pilot_test`
  decorator that calls `asyncio.run`. Use `App.run_test()`, drive input with
  `pilot.press(...)` or by setting `Input.value` directly and pressing
  enter, and always `await pilot.pause()` after an action before asserting.
- **`tests/test_engine_isolation.py`** — the engine/UI boundary guard
  described above. Don't add a Textual import to any engine module without
  expecting this to fail (correctly).

Prefer extending an existing test file's pattern over inventing a new
fixture shape — most of what you need (a `remote` fixture, a `clone` helper,
a `submit(pilot, text)` helper, `make_app(tmp_path, skills=...)`) already
exists in `test_sync.py` / `test_app.py`.

## Project philosophy

Carried over from the original build spec and worth keeping in mind for any
addition:

- **Entry speed is the product.** Any change that costs a keystroke on the
  common path — an extra confirmation prompt, a required field that used to
  be optional — is a regression, even if it makes some rarer path safer.
  This is why conflicts are surfaced in the echo line instead of a blocking
  dialog, and why `:undo`'s stack is deliberately not persisted rather than
  adding a confirmation step.
- **Append-only, always.** Session log files are never rewritten in normal
  operation — not even to "clean up" a malformed line. `skills.json` is the
  one exception (whole-file, rewritten in place), and that's only tolerable
  because it changes rarely and conflicts there are rare enough to resolve
  by hand.
- **Simplest thing that works.** No speculative configurability, no
  abstraction for a single call site, modules capped around ~200 lines
  before you stop and reconsider the design rather than let a module keep
  growing.
- **Ask before deviating** on the op-log format, the union-merge strategy,
  or the engine/UI separation — these three are what make the multi-machine
  guarantees hold. Everything else is fair game to just build.

## Ideas for extension

Deliberately not built, roughly in order of how much they'd add:

- **Units on metrics.** `distance` is a bare number with nowhere to say it
  means kilometres, and nothing converts between units. Would need either a
  units field on the metric declaration in `skills.json` or a convention
  (e.g. `distance_km`).
- **In-app querying.** Metrics are queryable today only via `:export csv`
  and whatever reads a CSV. There's no `:query distance > 10`, no filtering
  the detail list by a metric, no sorting by one. `stats.metric_totals` is
  the natural place to grow a small predicate/aggregation layer if this is
  wanted.
- **Metrics in the dashboard.** `SkillTable` only ever shows hours, level,
  streak, and the minutes-based sparkline — metrics currently surface only
  in the detail view and the export. Adding a metric column would mean
  deciding what to show for skills without that metric declared, and how
  many extra columns the table can carry before it stops fitting a normal
  terminal width.
- **Coloring the heatmap by a metric instead of minutes** — "which days did
  I run *far*," not "which days did I run *long*." `stats.bucket_thresholds`
  and `Heatmap.render` both currently assume the bucketed quantity is
  minutes; making that pluggable is the main work.
- **Goals and targets**, per skill or per metric. Ruled out explicitly in
  the original spec (`Explicitly out of scope: Goals/reminders/streak-
  protection mechanics`) — nothing in the data model anticipates them, and
  adding them well would mean deciding how a goal interacts with
  archiving, with multi-machine sync (is a goal itself syncable state?),
  and with the "entry speed is the product" rule.
- **Non-scalar metrics.** A metric value is one number or one string;
  `Op.from_dict` drops list/dict values on read rather than storing them
  (see the `isinstance(value, (int, float, str))` filter in `models.py`).
  Supporting e.g. a list of splits would need a real schema decision, not
  just relaxing that filter.
- **Resolving a `skills.json` conflict in-app.** Session logs union-merge
  automatically; `skills.json` is a whole-file git write, so two machines
  editing it at the same time produce real git conflict markers, and
  `store.load_skills` currently just reports the file as unreadable rather
  than offering an in-app merge UI. Rare in practice (skills change far
  less often than sessions do), and the current answer — fix it by hand in
  a text editor — has been good enough so far.
