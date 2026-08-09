"""Dashboard rows: hours, level, progress to the next decade, streak, sparkline."""

from __future__ import annotations

import datetime as dt

from textual.widgets import DataTable

from ..models import Skill
from ..stats import SPARKLINE_DAYS, Aggregate, current_streak, level_label, progress, sparkline_values

SPARK = " ▁▂▃▄▅▆▇█"
BAR_WIDTH = 10


def sparkline(values: list[int]) -> str:
    peak = max(values, default=0)
    if peak <= 0:
        return " " * len(values)
    return "".join(SPARK[min(8, round(value * 8 / peak))] for value in values)


def progress_bar(fraction: float) -> str:
    filled = round(fraction * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class SkillTable(DataTable):
    """Not focusable: the command bar is the only place input can go."""

    can_focus = False

    def on_mount(self) -> None:
        self.cursor_type = "none"
        self.show_cursor = False
        self.add_columns("skill", "hours", "level", "progress", "streak", f"last {SPARKLINE_DAYS}d")

    def update_rows(self, skills: list[Skill], agg: Aggregate, today: dt.date) -> None:
        self.clear()
        for label, skill_id in self._ordered(skills, agg):
            minutes = agg.minutes_by_skill.get(skill_id, 0)
            hours = minutes / 60
            days = set(agg.daily_by_skill.get(skill_id, {}))
            streak = current_streak(days, today)
            self.add_row(
                label,
                f"{hours:,.1f}",
                level_label(hours),
                f"{progress_bar(progress(hours))} {progress(hours) * 100:3.0f}%",
                f"{streak}d" if streak else "-",
                sparkline(sparkline_values(agg.daily_by_skill.get(skill_id, {}), today)),
            )

    def _ordered(self, skills: list[Skill], agg: Aggregate) -> list[tuple[str, str]]:
        """Visible skills plus any skill the log references but skills.json has
        lost - dropping those would silently hide real hours."""
        rows = [(skill.name, skill.id) for skill in skills if not skill.archived]
        rows += [(f"⟨{skill_id}⟩", skill_id) for skill_id in sorted(agg.unknown_skills)]
        rows.sort(key=lambda row: agg.minutes_by_skill.get(row[1], 0), reverse=True)
        return rows
