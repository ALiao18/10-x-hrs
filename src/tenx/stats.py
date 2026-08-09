"""Derived values. Nothing here is ever stored.

Everything the dashboard needs is computed in a single pass over sessions
(`aggregate`), so adding a skill row never rescans the session list.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

from .models import Session, Skill

FIXED_CUTS = (30, 60, 120)
QUANTILE_MIN_DAYS = 30
SPARKLINE_DAYS = 30
GRID_WEEKS = 53


@dataclass
class Aggregate:
    """One pass over every live session."""

    minutes_by_skill: dict[str, int] = field(default_factory=dict)
    count_by_skill: dict[str, int] = field(default_factory=dict)
    daily: dict[dt.date, int] = field(default_factory=dict)
    daily_by_skill: dict[str, dict[dt.date, int]] = field(default_factory=dict)
    thresholds: tuple[int, int, int] = FIXED_CUTS
    unknown_skills: set[str] = field(default_factory=set)


def aggregate(sessions: dict[str, Session], skills: list[Skill]) -> Aggregate:
    """Roll sessions up.

    `daily` (which drives the heatmap and the global streak) covers visible
    skills only; archived skills keep their own totals and their own per-skill
    daily series, but no longer colour the consistency view.
    """
    known = {s.id for s in skills}
    archived = {s.id for s in skills if s.archived}
    agg = Aggregate()
    for session in sessions.values():
        skill = session.skill
        agg.minutes_by_skill[skill] = agg.minutes_by_skill.get(skill, 0) + session.minutes
        agg.count_by_skill[skill] = agg.count_by_skill.get(skill, 0) + 1
        per_skill = agg.daily_by_skill.setdefault(skill, {})
        per_skill[session.date] = per_skill.get(session.date, 0) + session.minutes
        if skill not in known:
            agg.unknown_skills.add(skill)
        if skill not in archived:
            agg.daily[session.date] = agg.daily.get(session.date, 0) + session.minutes
    agg.thresholds = bucket_thresholds(agg.daily)
    return agg


# --- levels -----------------------------------------------------------------


def level(hours: float) -> int:
    """floor(log10(hours)) for hours >= 1, else 0 - by digit count, so 1000h
    can never land on level 2 through float error."""
    if hours < 1:
        return 0
    return len(str(int(hours))) - 1


def progress(hours: float) -> float:
    """Fraction of the way to the next power of ten, clamped to [0, 1]."""
    if hours <= 0:
        return 0.0
    if hours < 1:
        return hours
    current = level(hours)
    low, high = 10.0**current, 10.0 ** (current + 1)
    return min(1.0, max(0.0, (hours - low) / (high - low)))


def level_label(hours: float) -> str:
    superscript = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    return "10" + str(level(hours)).translate(superscript)


# --- streaks ----------------------------------------------------------------


def current_streak(days: set[dt.date], today: dt.date) -> int:
    """Consecutive days ending today or yesterday - an unlogged today does not
    reset the count before you have had a chance to practise."""
    cursor = today if today in days else today - dt.timedelta(days=1)
    if cursor not in days:
        return 0
    length = 0
    while cursor in days:
        length += 1
        cursor -= dt.timedelta(days=1)
    return length


def longest_streak(days: set[dt.date]) -> int:
    best = run = 0
    previous: dt.date | None = None
    for day in sorted(days):
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return best


# --- heatmap buckets --------------------------------------------------------


def bucket_thresholds(daily: dict[dt.date, int]) -> tuple[int, int, int]:
    """p25/p50/p75 of non-zero days, or fixed cuts below 30 samples.

    Forced strictly increasing: identical daily totals collapse the three
    quantiles onto one value, which would leave `bucket` jumping from 1 to 4
    with nothing in between.
    """
    values = sorted(v for v in daily.values() if v > 0)
    if len(values) < QUANTILE_MIN_DAYS:
        return FIXED_CUTS
    cuts = statistics.quantiles(values, n=4, method="inclusive")
    first = int(round(cuts[0]))
    second = max(int(round(cuts[1])), first + 1)
    third = max(int(round(cuts[2])), second + 1)
    return (first, second, third)


def bucket(minutes: int, thresholds: tuple[int, int, int]) -> int:
    """0 for an empty day, 1-4 otherwise."""
    if minutes <= 0:
        return 0
    first, second, third = thresholds
    if minutes <= first:
        return 1
    if minutes <= second:
        return 2
    if minutes <= third:
        return 3
    return 4


# --- grids ------------------------------------------------------------------


def year_grid(year: int) -> list[list[dt.date | None]]:
    """53 week-columns x 7 weekday-rows (Monday first).

    Columns start on the Monday on or before 1 January, so 53 columns always
    span the whole year including leap days and ISO week-53 years. Days outside
    the year are None.
    """
    jan_first = dt.date(year, 1, 1)
    start = jan_first - dt.timedelta(days=jan_first.weekday())
    grid = []
    for week in range(GRID_WEEKS):
        column: list[dt.date | None] = []
        for weekday in range(7):
            day = start + dt.timedelta(days=week * 7 + weekday)
            column.append(day if day.year == year else None)
        grid.append(column)
    return grid


def month_labels(grid: list[list[dt.date | None]]) -> list[tuple[int, str]]:
    """(column index, short month name) for the header row.

    Takes a grid rather than a year so the heatmap can label a clipped view
    without the indices drifting out from under it.
    """
    labels: list[tuple[int, str]] = []
    seen: set[int] = set()
    for index, column in enumerate(grid):
        for day in column:
            if day is not None and day.month not in seen:
                seen.add(day.month)
                labels.append((index, day.strftime("%b")))
                break
    return labels


def sparkline_values(daily: dict[dt.date, int], today: dt.date, days: int = SPARKLINE_DAYS) -> list[int]:
    """Minutes per day for the trailing window, oldest first."""
    start = today - dt.timedelta(days=days - 1)
    return [daily.get(start + dt.timedelta(days=offset), 0) for offset in range(days)]


def recent_sessions(sessions: dict[str, Session], skill: str | None = None, limit: int = 20) -> list[Session]:
    """Newest first - the list `:rm <n>` and `:edit <n>` index into."""
    pool = [s for s in sessions.values() if skill is None or s.skill == skill]
    pool.sort(key=lambda s: (s.date, s.id), reverse=True)
    return pool[:limit]
