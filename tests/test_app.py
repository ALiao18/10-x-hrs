"""Textual pilot smoke tests.

`App.run_test()` needs no pytest plugin, so each scenario is an async body
driven by asyncio.run from a plain sync test.
"""

import asyncio
import csv
import datetime as dt
import functools

from textual.widgets import Input

from tenx import store
from tenx.app import TenxApp
from tenx.config import init
from tenx.models import Skill
from tenx.stats import year_grid
from tenx.widgets import CommandBar, DetailPanel, Heatmap, SkillTable


def pilot_test(func):
    """Run an async test body without pytest-asyncio."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def make_app(tmp_path, skills=("ml", "lc")):
    init(root=tmp_path, device_id="test")
    store.save_skills(
        tmp_path,
        [
            Skill(sid, {"ml": "machine learning", "lc": "leetcode"}.get(sid, sid), "2026-01-01")
            for sid in skills
        ],
    )
    app = TenxApp(tmp_path)
    app.config.auto_sync = False  # keep git out of the UI tests
    return app


def cells(app):
    table = app.query_one(SkillTable)
    return [
        [str(table.get_cell_at((row, col))) for col in range(len(table.columns))]
        for row in range(table.row_count)
    ]


async def submit(pilot, text):
    pilot.app.query_one(Input).value = text
    await pilot.press("enter")
    await pilot.pause()


# --- boot -------------------------------------------------------------------


@pilot_test
async def test_boots_on_an_empty_data_dir(tmp_path):
    app = TenxApp(tmp_path)
    app.config.auto_sync = False
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(SkillTable).row_count == 0
        assert app.query_one(Input).has_focus


@pilot_test
async def test_the_command_bar_is_the_only_focusable_widget(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h")
        assert app.query_one(Input).has_focus
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one(Input).has_focus, "tab must not move focus off the entry"
        assert not app.query_one(SkillTable).can_focus
        assert not app.query_one(Heatmap).can_focus


# --- quick add --------------------------------------------------------------


@pilot_test
async def test_a_quick_add_updates_the_table_and_todays_cell(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        before = app.query_one(Heatmap).daily.get(app.today, 0)
        await submit(pilot, "ml 1h30")

        row = next(r for r in cells(app) if r[0] == "machine learning")
        assert row[1] == "1.5"
        assert app.query_one(Heatmap).daily.get(app.today, 0) == before + 90
        assert app.query_one(Input).value == "", "a successful add clears the entry"
        assert "1.5h total" in app.query_one(CommandBar).last_message

        # And it is on disk, not just in memory.
        assert len(store.load(tmp_path).sessions) == 1


@pilot_test
async def test_invalid_input_is_left_in_place_to_fix(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h3o sweep")
        assert app.query_one(Input).value == "ml 1h3o sweep"
        assert app.query_one(Input).has_focus
        echo = app.query_one(CommandBar).last_message
        assert echo.startswith("✗") and "1h30" in echo
        assert store.load(tmp_path).sessions == {}


@pilot_test
async def test_backfill_with_a_date(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "lc 45m yesterday deep work")
        session = next(iter(store.load(tmp_path).sessions.values()))
        assert session.date == app.today - dt.timedelta(days=1)
        assert session.note == "deep work"


# --- commands ---------------------------------------------------------------


@pilot_test
async def test_new_and_rename_and_archive(tmp_path):
    app = make_app(tmp_path, skills=())
    async with app.run_test() as pilot:
        await submit(pilot, ":new poker no limit holdem")
        assert [s.name for s in store.load_skills(tmp_path)] == ["no limit holdem"]

        await submit(pilot, ":rename poker tournament poker")
        assert [s.name for s in store.load_skills(tmp_path)] == ["tournament poker"]

        await submit(pilot, "poker 2h")
        await submit(pilot, ":archive poker")
        assert store.load_skills(tmp_path)[0].archived
        assert [row[0] for row in cells(app)] == [], "archived skills leave the dashboard"
        assert app.agg.minutes_by_skill["poker"] == 120, "but keep their all-time total"
        assert app.query_one(Heatmap).daily == {}, "and leave the heatmap"

        await submit(pilot, ":unarchive poker")
        assert [row[0] for row in cells(app)] == ["tournament poker"]


@pilot_test
async def test_detail_then_remove_by_index(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h one")
        await submit(pilot, "ml 2h two")
        await submit(pilot, "ml 3h three")

        await submit(pilot, "d ml")
        assert app.query_one(DetailPanel).display
        assert len(app.last_list) == 3

        doomed = app.last_list[1]
        await submit(pilot, ":rm 2")
        reloaded = store.load(tmp_path).sessions
        assert doomed not in reloaded
        assert len(reloaded) == 2
        # The original add line is untouched; a tombstone was appended.
        raw = store.log_path(tmp_path, "test").read_text()
        assert raw.count(doomed) == 2 and '"op":"del"' in raw

        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one(DetailPanel).display


@pilot_test
async def test_edit_and_undo(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h")
        await submit(pilot, ":edit 1 2h30")
        assert next(iter(store.load(tmp_path).sessions.values())).minutes == 150

        await submit(pilot, ":undo")
        assert store.load(tmp_path).sessions == {}

        await submit(pilot, ":undo")
        assert "nothing to undo" in app.query_one(CommandBar).last_message


@pilot_test
async def test_undo_stack_is_empty_after_a_restart(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h")

    restarted = TenxApp(tmp_path)
    restarted.config.auto_sync = False
    async with restarted.run_test() as pilot:
        await submit(pilot, ":undo")
        echo = restarted.query_one(CommandBar).last_message
        assert "nothing to undo" in echo
        assert len(store.load(tmp_path).sessions) == 1, "must not delete something older"


@pilot_test
async def test_filter_and_year_scope_the_heatmap(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h")
        await submit(pilot, "lc 30m")
        assert app.query_one(Heatmap).daily[app.today] == 90

        await submit(pilot, ":filter lc")
        assert app.query_one(Heatmap).daily[app.today] == 30

        await submit(pilot, ":filter off")
        assert app.query_one(Heatmap).daily[app.today] == 90

        await submit(pilot, ":year 2024")
        assert app.query_one(Heatmap).year == 2024


@pilot_test
async def test_export_csv(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h30 sweep")
        await submit(pilot, f":export csv {tmp_path / 'out.csv'}")
        text = (tmp_path / "out.csv").read_text()
        assert "sweep" in text and "ml" in text


@pilot_test
async def test_unknown_command_and_bad_args_do_not_crash(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        for bad in (":nope", ":new", ":rm 9", ":filter zzz", ":edit 1 abc", ":detail zzz"):
            await submit(pilot, bad)
            assert app.query_one(CommandBar).last_message.startswith("✗"), bad
            assert app.query_one(Input).has_focus


# --- rendering --------------------------------------------------------------


@pilot_test
async def test_a_skill_missing_from_skills_json_still_shows_its_hours(tmp_path):
    app = make_app(tmp_path)
    store.append_op(
        store.log_path(tmp_path, "test"),
        store.make_add("ghost", dt.date(2026, 1, 5), 240, "recovered log"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        row = next(r for r in cells(app) if r[0] == "⟨ghost⟩")
        assert row[1] == "4.0"


@pilot_test
async def test_renders_a_full_year_at_100_columns(tmp_path):
    app = make_app(tmp_path)
    path = store.log_path(tmp_path, "test")
    start = dt.date(app.today.year, 1, 1)
    for offset in range(0, 360, 2):
        store.append_op(path, store.make_add("ml", start + dt.timedelta(days=offset), 30 + offset % 90))

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        rendered = app.query_one(Heatmap).render()
        lines = rendered.plain.rstrip("\n").split("\n")
        assert len(lines) == 8  # month header + 7 weekday rows
        assert "Jan" in lines[0] and "Dec" in lines[0], lines[0]
        assert len(set(span.style for span in rendered.spans)) > 2, "buckets should differ"


@pilot_test
async def test_narrow_terminal_clips_weeks_from_the_left(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(40, 30)) as pilot:
        await pilot.pause()
        heatmap = app.query_one(Heatmap)
        assert heatmap.size.width < 4 + 53, "this terminal is too narrow for a full year"
        lines = heatmap.render().plain.rstrip("\n").split("\n")
        assert len(lines) == 8
        assert all(len(line) <= heatmap.size.width for line in lines), "must not overflow"

        full = year_grid(app.today.year)
        shown = heatmap.visible_columns()
        assert len(shown) < len(full), "a narrow terminal must actually clip"
        clipped_from = len(full) - len(shown)
        assert shown == full[clipped_from:], "weeks are dropped from the left, not the right"
        assert shown[-1] == full[-1], "the most recent week stays visible"


# --- conflicts --------------------------------------------------------------

CONFLICT_ID = "01AAAAAAAAAAAAAAAAAAAAAAAA"
LOGGED = dt.date(2026, 8, 9)
# Well in the past, so a resolution written "now" always sorts newer than the
# conflict it settles - otherwise the fixture, not the code, decides the winner.
LOGGED_AT = "2020-01-01T10:00:00Z"
SAME_SECOND = "2020-01-01T11:00:00Z"
LATER = "2020-01-01T12:00:00Z"


def plant_field_conflict(tmp_path, here="test", there="laptop", ours=105, theirs=120):
    """Two machines writing the same field in the same second: the only case
    where the sort key, not the clock, decides the winner."""
    from tenx.models import Op

    fields = {"skill": "ml", "date": LOGGED, "minutes": 60, "note": ""}
    store.append_op(store.log_path(tmp_path, here), Op("add", CONFLICT_ID, LOGGED_AT, fields))
    store.append_op(store.log_path(tmp_path, here), Op("edit", CONFLICT_ID, SAME_SECOND, {"minutes": ours}))
    store.append_op(
        store.log_path(tmp_path, there), Op("edit", CONFLICT_ID, SAME_SECOND, {"minutes": theirs})
    )


def plant_deleted_conflict(tmp_path):
    """Deleted on one machine, edited afterwards on another. The tombstone wins
    by rule rather than by recency, so a newer intent gets dropped."""
    from tenx.models import Op

    fields = {"skill": "ml", "date": LOGGED, "minutes": 90, "note": "sweep"}
    store.append_op(store.log_path(tmp_path, "test"), Op("add", CONFLICT_ID, LOGGED_AT, fields))
    store.append_op(store.log_path(tmp_path, "test"), Op("del", CONFLICT_ID, SAME_SECOND))
    store.append_op(store.log_path(tmp_path, "laptop"), Op("edit", CONFLICT_ID, LATER, {"minutes": 150}))


def minutes_now(tmp_path):
    return store.load(tmp_path).sessions[CONFLICT_ID].minutes


@pilot_test
async def test_a_conflict_is_raised_on_the_entry_itself(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, "ml 1h")
        echo = app.query_one(CommandBar).last_message
        assert echo.startswith("✓"), "a conflict must never block a log"
        assert "1 conflict" in echo and ":conflicts" in echo
        assert "1 conflict" in app._header(), "and it stays visible in the status line"
        assert len(store.load(tmp_path).sessions) == 2, "the entry still landed"


@pilot_test
async def test_conflicts_lists_both_sides(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        panel = app.query_one(DetailPanel)
        assert panel.display and len(app.conflict_list) == 1
        body = panel.render().plain
        assert "test" in body and "laptop" in body
        assert "1h45" in body and "2h" in body  # 105 and 120 minutes
        assert ":fix 1 mine | theirs | newest | oldest" in body


@pilot_test
async def test_fix_theirs(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        await submit(pilot, ":fix 1 theirs")
        assert minutes_now(tmp_path) == 120
        assert store.load(tmp_path).collisions == [], "resolving must settle it for good"
        assert app.conflict_list == []


@pilot_test
async def test_fix_mine(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path, ours=45, theirs=60)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        await submit(pilot, ":fix 1 mine")
        assert minutes_now(tmp_path) == 45
        assert store.load(tmp_path).collisions == []


@pilot_test
async def test_fix_newest_and_oldest(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        loser = app.conflict_list[0].loser.value
        await submit(pilot, ":fix 1 oldest")
        assert minutes_now(tmp_path) == loser
        assert store.load(tmp_path).collisions == []


@pilot_test
async def test_fix_falls_back_when_neither_side_is_this_machine(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path, here="laptop", there="phone")
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        await submit(pilot, ":fix 1 mine")
        message = app.query_one(CommandBar).last_message
        assert message.startswith("✗") and "newest or oldest" in message
        await submit(pilot, ":fix 1 newest")
        assert store.load(tmp_path).collisions == []


@pilot_test
async def test_keep_restores_a_session_deleted_elsewhere(tmp_path):
    app = make_app(tmp_path)
    plant_deleted_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        assert app.conflict_list[0].kind == "deleted"
        await submit(pilot, ":fix 1 keep")

        loaded = store.load(tmp_path)
        assert loaded.collisions == []
        assert CONFLICT_ID not in loaded.sessions, "tombstones are absorbing"
        restored = list(loaded.sessions.values())
        assert len(restored) == 1
        assert (restored[0].minutes, restored[0].note) == (150, "sweep")


@pilot_test
async def test_drop_confirms_the_deletion(tmp_path):
    app = make_app(tmp_path)
    plant_deleted_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        await submit(pilot, ":fix 1 drop")
        loaded = store.load(tmp_path)
        assert loaded.collisions == [] and loaded.sessions == {}


@pilot_test
async def test_the_wrong_verb_for_the_kind_explains_itself(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":conflicts")
        await submit(pilot, ":fix 1 keep")
        message = app.query_one(CommandBar).last_message
        assert message.startswith("✗") and "mine/theirs/newest/oldest" in message
        assert store.load(tmp_path).collisions != [], "a bad verb must not settle anything"


@pilot_test
async def test_fix_without_listing_first(tmp_path):
    app = make_app(tmp_path)
    plant_field_conflict(tmp_path)
    async with app.run_test() as pilot:
        await submit(pilot, ":fix 1 mine")
        assert "run :conflicts first" in app.query_one(CommandBar).last_message


# --- custom metrics ---------------------------------------------------------


@pilot_test
async def test_declaring_a_metric_then_logging_it(tmp_path):
    app = make_app(tmp_path, skills=("run", "ml"))
    async with app.run_test() as pilot:
        await submit(pilot, "run 45m distance=8.2")
        session = next(iter(store.load(tmp_path).sessions.values()))
        assert session.extra == {}, "undeclared, so it is still just note text"
        assert session.note == "distance=8.2"

        await submit(pilot, ":metric run distance")
        assert store.load_skills(tmp_path)[0].metrics == ("distance",)

        await submit(pilot, "run 50m distance=8.2 easy")
        logged = [s for s in store.load(tmp_path).sessions.values() if s.minutes == 50]
        assert logged[0].extra == {"distance": 8.2}
        assert logged[0].note == "easy"


@pilot_test
async def test_metrics_show_in_the_detail_view_and_total_up(tmp_path):
    app = make_app(tmp_path, skills=("run",))
    async with app.run_test() as pilot:
        await submit(pilot, ":metric run distance")
        await submit(pilot, "run 45m distance=8.2 easy")
        await submit(pilot, "run 60m distance=11.8 yesterday")
        await submit(pilot, "d run")

        body = app.query_one(DetailPanel).render().plain
        assert "distance 8.2" in body and "distance 11.8" in body
        assert "distance 20" in body, "the heading totals the numeric ones"


@pilot_test
async def test_a_metric_is_queryable_through_the_csv_export(tmp_path):
    app = make_app(tmp_path, skills=("run", "ml"))
    async with app.run_test() as pilot:
        await submit(pilot, ":metric run distance")
        await submit(pilot, ":metric run shoes")
        await submit(pilot, "run 45m distance=8.2 shoes=vaporfly")
        await submit(pilot, "ml 1h sweep")
        await submit(pilot, f":export csv {tmp_path / 'out.csv'}")

        rows = list(csv.reader((tmp_path / "out.csv").read_text().splitlines()))
        assert rows[0] == ["date", "skill", "minutes", "note", "id", "distance", "shoes"]
        by_skill = {row[1]: row for row in rows[1:]}
        assert by_skill["run"][5:] == ["8.2", "vaporfly"]
        assert by_skill["ml"][5:] == ["", ""], "a skill without the metric leaves it blank"


@pilot_test
async def test_undeclaring_a_metric_keeps_what_was_already_logged(tmp_path):
    app = make_app(tmp_path, skills=("run",))
    async with app.run_test() as pilot:
        await submit(pilot, ":metric run distance")
        await submit(pilot, "run 45m distance=8.2")
        await submit(pilot, ":metric run -distance")

        assert store.load_skills(tmp_path)[0].metrics == ()
        session = next(iter(store.load(tmp_path).sessions.values()))
        assert session.extra == {"distance": 8.2}, "the logged value is untouched"

        await submit(pilot, f":export csv {tmp_path / 'out.csv'}")
        assert "distance" in (tmp_path / "out.csv").read_text().splitlines()[0]


@pilot_test
async def test_metric_command_errors(tmp_path):
    app = make_app(tmp_path, skills=("run",))
    async with app.run_test() as pilot:
        for bad in (":metric nosuch distance", ":metric run -never-declared", ":metric run 9lives"):
            await submit(pilot, bad)
            assert app.query_one(CommandBar).last_message.startswith("✗"), bad


@pilot_test
async def test_edit_changes_a_metric_as_well_as_a_duration(tmp_path):
    app = make_app(tmp_path, skills=("run",))
    async with app.run_test() as pilot:
        await submit(pilot, ":metric run distance")
        await submit(pilot, "run 45m distance=8.2 easy")
        await submit(pilot, "d run")

        await submit(pilot, ":edit 1 distance=8.6")
        session = next(iter(store.load(tmp_path).sessions.values()))
        assert session.extra == {"distance": 8.6}
        assert session.minutes == 45 and session.note == "easy", "only the metric moved"

        await submit(pilot, ":edit 1 50m")
        session = next(iter(store.load(tmp_path).sessions.values()))
        assert session.minutes == 50 and session.extra == {"distance": 8.6}
