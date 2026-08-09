"""Derived values: levels, streaks, buckets, and the heatmap grid."""

import datetime as dt
import time

import pytest

from tenx.models import Session, Skill
from tenx.stats import (
    FIXED_CUTS,
    aggregate,
    bucket,
    bucket_thresholds,
    current_streak,
    level,
    level_label,
    longest_streak,
    month_labels,
    progress,
    sparkline_values,
    year_grid,
)

TODAY = dt.date(2026, 8, 9)


def session(sid, skill="ml", day=TODAY, minutes=60):
    return Session(id=sid, skill=skill, date=day, minutes=minutes)


def days_before(count, end=TODAY):
    return {end - dt.timedelta(days=offset) for offset in range(count)}


# --- levels -----------------------------------------------------------------


@pytest.mark.parametrize(
    "hours,expected",
    [(0, 0), (0.5, 0), (1, 0), (9.99, 0), (10, 1), (99, 1), (100, 2), (999.9, 2), (1000, 3), (10000, 4)],
)
def test_level(hours, expected):
    assert level(hours) == expected


def test_level_is_exact_at_the_powers_of_ten():
    """floor(log10(1000)) can come back as 2 in floating point; digit counting
    cannot."""
    for exponent in range(6):
        assert level(float(10**exponent)) == exponent


def test_level_label():
    assert level_label(0.5) == "10⁰"
    assert level_label(614.0) == "10²"


@pytest.mark.parametrize(
    "hours,expected",
    [(0, 0.0), (0.5, 0.5), (1, 0.0), (5.5, 0.5), (10, 0.0), (55, 0.5), (100, 0.0), (550, 0.5)],
)
def test_progress(hours, expected):
    assert progress(hours) == pytest.approx(expected)


def test_progress_is_always_in_range():
    for hours in (0, 0.01, 0.99, 1, 9.99, 10, 5000, 99999):
        assert 0.0 <= progress(hours) <= 1.0


# --- streaks ----------------------------------------------------------------


def test_unbroken_streak():
    assert current_streak(days_before(5), TODAY) == 5


def test_gap_breaks_the_streak():
    days = days_before(3) | {TODAY - dt.timedelta(days=9)}
    assert current_streak(days, TODAY) == 3


def test_yesterday_keeps_the_streak_alive():
    days = days_before(4, end=TODAY - dt.timedelta(days=1))
    assert current_streak(days, TODAY) == 4


def test_two_silent_days_reset_the_streak():
    days = days_before(4, end=TODAY - dt.timedelta(days=2))
    assert current_streak(days, TODAY) == 0


def test_single_day_and_empty():
    assert current_streak({TODAY}, TODAY) == 1
    assert current_streak(set(), TODAY) == 0


def test_longest_streak():
    days = {TODAY - dt.timedelta(days=n) for n in (0, 1, 5, 6, 7, 8, 20)}
    assert longest_streak(days) == 4
    assert longest_streak(set()) == 0
    assert longest_streak({TODAY}) == 1


# --- buckets ----------------------------------------------------------------


def test_fixed_cuts_below_thirty_nonzero_days():
    daily = {TODAY - dt.timedelta(days=n): 45 for n in range(29)}
    assert bucket_thresholds(daily) == FIXED_CUTS


def test_quantiles_from_thirty_nonzero_days():
    daily = {TODAY - dt.timedelta(days=n): (n + 1) * 10 for n in range(40)}
    cuts = bucket_thresholds(daily)
    assert cuts != FIXED_CUTS
    assert cuts[0] < cuts[1] < cuts[2]


def test_zero_days_are_excluded_from_the_sample():
    daily = {TODAY - dt.timedelta(days=n): (60 if n % 2 else 0) for n in range(80)}
    assert bucket_thresholds(daily) == (60, 61, 62)


def test_degenerate_quantiles_stay_usable():
    """Identical daily totals collapse p25/p50/p75 onto one value. That must
    still yield strictly increasing cuts and a valid bucket for every input."""
    daily = {TODAY - dt.timedelta(days=n): 45 for n in range(40)}
    cuts = bucket_thresholds(daily)
    assert cuts[0] < cuts[1] < cuts[2]
    for minutes in (0, 1, 45, 46, 1000):
        assert bucket(minutes, cuts) in range(5)
    assert bucket(45, cuts) == 1


@pytest.mark.parametrize(
    "minutes,expected", [(0, 0), (-5, 0), (1, 1), (30, 1), (31, 2), (60, 2), (61, 3), (120, 3), (121, 4)]
)
def test_bucket_edges(minutes, expected):
    assert bucket(minutes, FIXED_CUTS) == expected


# --- the grid ---------------------------------------------------------------


@pytest.mark.parametrize("year", [2020, 2021, 2023, 2024, 2026, 2100])
def test_year_grid_covers_exactly_the_year(year):
    """53 columns of pure day arithmetic from the Monday on or before 1 Jan.

    Leap days and ISO week-53 years (2021-01-01 is ISO 2020-W53) are non-events
    here because no week-numbering function is involved.
    """
    grid = year_grid(year)
    assert len(grid) == 53 and all(len(column) == 7 for column in grid)
    seen = [day for column in grid for day in column if day is not None]
    expected = (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days
    assert len(seen) == len(set(seen)) == expected
    assert min(seen) == dt.date(year, 1, 1) and max(seen) == dt.date(year, 12, 31)


def test_weekday_rows_are_consistent():
    for column in year_grid(2026):
        for row, day in enumerate(column):
            if day is not None:
                assert day.weekday() == row


def test_year_boundary_lands_in_the_right_column():
    grid = year_grid(2026)
    last = [
        (col, row)
        for col, column in enumerate(grid)
        for row, day in enumerate(column)
        if day == dt.date(2026, 12, 31)
    ]
    assert last and last[0][0] >= 52
    grid_next = year_grid(2027)
    first = [
        (col, row)
        for col, column in enumerate(grid_next)
        for row, day in enumerate(column)
        if day == dt.date(2027, 1, 1)
    ]
    assert first and first[0][0] == 0


def test_leap_day_is_present():
    assert dt.date(2024, 2, 29) in [day for column in year_grid(2024) for day in column]
    assert dt.date(2023, 2, 28) in [day for column in year_grid(2023) for day in column]


def test_month_labels():
    labels = month_labels(year_grid(2026))
    assert len(labels) == 12
    assert labels[0][1] == "Jan" and labels[-1][1] == "Dec"
    assert [col for col, _ in labels] == sorted(col for col, _ in labels)


def test_month_labels_reindex_for_a_clipped_grid():
    """The heatmap clips weeks off the left; labels must follow, not drift."""
    clipped = year_grid(2026)[-10:]
    labels = month_labels(clipped)
    assert labels[0][0] == 0
    assert all(0 <= col < len(clipped) for col, _ in labels)


def test_sparkline_values():
    daily = {TODAY: 60, TODAY - dt.timedelta(days=2): 30}
    values = sparkline_values(daily, TODAY, days=5)
    assert values == [0, 0, 30, 0, 60]


# --- aggregation ------------------------------------------------------------


def test_aggregate_is_one_pass_over_sessions():
    skills = [Skill("ml", "machine learning", "2026-01-01"), Skill("lc", "leetcode", "2026-01-01")]
    sessions = {
        "A": session("A", "ml", TODAY, 90),
        "B": session("B", "ml", TODAY, 30),
        "C": session("C", "lc", TODAY - dt.timedelta(days=1), 45),
    }
    agg = aggregate(sessions, skills)
    assert agg.minutes_by_skill == {"ml": 120, "lc": 45}
    assert agg.count_by_skill == {"ml": 2, "lc": 1}
    assert agg.daily == {TODAY: 120, TODAY - dt.timedelta(days=1): 45}
    assert agg.daily_by_skill["ml"][TODAY] == 120


def test_archived_skills_keep_their_total_but_leave_the_heatmap():
    skills = [Skill("ml", "machine learning", "2026-01-01"), Skill("old", "old", "2026-01-01", archived=True)]
    sessions = {"A": session("A", "ml", TODAY, 60), "B": session("B", "old", TODAY, 120)}
    agg = aggregate(sessions, skills)
    assert agg.minutes_by_skill["old"] == 120  # still counted all-time
    assert agg.daily == {TODAY: 60}  # but no longer colours the heatmap
    assert agg.daily_by_skill["old"][TODAY] == 120  # its own detail view still works


def test_sessions_for_a_skill_missing_from_skills_json_are_kept():
    agg = aggregate({"A": session("A", "ghost", TODAY, 60)}, [])
    assert agg.unknown_skills == {"ghost"}
    assert agg.minutes_by_skill["ghost"] == 60
    assert agg.daily == {TODAY: 60}


# --- timezones --------------------------------------------------------------


def test_a_timezone_move_never_shifts_a_stored_date(tmp_path, monkeypatch):
    """`date` is a literal local calendar date and is never recomputed, so two
    machines eight and four hours off UTC land on the same day, and relocating
    afterwards moves nothing."""
    from tenx.models import Op
    from tenx.store import append_op, log_path, read_ops, replay

    # Same local date, wildly different UTC wall-clock times.
    append_op(
        log_path(tmp_path, "singapore"),
        Op(
            op="add", id="SG", ts="2026-08-09T01:00:00Z", fields={"skill": "ml", "date": TODAY, "minutes": 60}
        ),
    )
    append_op(
        log_path(tmp_path, "newyork"),
        Op(
            op="add", id="NY", ts="2026-08-10T03:00:00Z", fields={"skill": "ml", "date": TODAY, "minutes": 30}
        ),
    )

    seen = []
    for zone in ("UTC", "Asia/Singapore", "America/New_York", "Pacific/Auckland"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        sessions, _ = replay(read_ops(tmp_path)[0])
        seen.append({s.id: s.date for s in sessions.values()})
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()

    assert all(snapshot == seen[0] for snapshot in seen)
    assert seen[0] == {"SG": TODAY, "NY": TODAY}

    agg = aggregate(replay(read_ops(tmp_path)[0])[0], [Skill("ml", "ml", "2026-01-01")])
    assert agg.daily == {TODAY: 90}, "same local date must be one bucket, not two"


# --- scale ------------------------------------------------------------------


def test_five_thousand_sessions_load_and_aggregate_quickly(tmp_path):
    from tenx.models import Op
    from tenx.store import append_op, load, log_path

    path = log_path(tmp_path, "mac")
    for i in range(5000):
        append_op(
            path,
            Op(
                op="add",
                id=f"{i:026d}",
                ts=f"2026-08-09T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z",
                fields={
                    "skill": f"s{i % 8}",
                    "date": TODAY - dt.timedelta(days=i % 900),
                    "minutes": 15 + (i % 120),
                },
            ),
        )
    skills = [Skill(f"s{n}", f"skill {n}", "2024-01-01") for n in range(8)]

    start = time.perf_counter()
    result = load(tmp_path)
    agg = aggregate(result.sessions, skills)
    elapsed = time.perf_counter() - start

    assert len(result.sessions) == 5000
    assert sum(agg.minutes_by_skill.values()) == sum(15 + (i % 120) for i in range(5000))
    assert elapsed < 0.5, f"load+aggregate took {elapsed * 1000:.0f}ms"
