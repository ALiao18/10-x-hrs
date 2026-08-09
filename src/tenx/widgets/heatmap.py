"""The 53x7 contribution grid."""

from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual.widgets import Static

from ..stats import FIXED_CUTS, bucket, month_labels, year_grid

LEVEL_COLORS = ("#20242b", "#0e4429", "#006d32", "#26a641", "#39d353")
CELL = "█"
# One column per week: 53 weeks plus the weekday gutter fits a full year
# inside 100 columns, which is the width the heatmap has to render at.
CELL_WIDTH = 1
LABEL_WIDTH = 4
ROW_LABELS = ("Mon", "", "Wed", "", "Fri", "", "")


class Heatmap(Static):
    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.daily: dict[dt.date, int] = {}
        self.thresholds: tuple[int, int, int] = FIXED_CUTS
        self.year = dt.date.today().year

    def update_data(self, daily: dict[dt.date, int], thresholds: tuple[int, int, int], year: int) -> None:
        self.daily = daily
        self.thresholds = thresholds
        self.year = year
        self.refresh()

    def visible_columns(self) -> list[list[dt.date | None]]:
        """The grid clipped to fit. Weeks are dropped from the left, so the
        most recent week is the one that always survives."""
        available = max(1, (max(self.size.width, LABEL_WIDTH + CELL_WIDTH) - LABEL_WIDTH) // CELL_WIDTH)
        return year_grid(self.year)[-available:]

    def render(self) -> Text:
        columns = self.visible_columns()
        text = Text()
        text.append(" " * LABEL_WIDTH)
        text.append(self._month_header(columns), style="dim")
        text.append("\n")
        for row in range(7):
            text.append(f"{ROW_LABELS[row]:<{LABEL_WIDTH}}", style="dim")
            for column in columns:
                day = column[row]
                if day is None:
                    text.append(" " * CELL_WIDTH)
                else:
                    text.append(CELL, style=LEVEL_COLORS[bucket(self.daily.get(day, 0), self.thresholds)])
            text.append("\n")
        return text

    @staticmethod
    def _month_header(columns: list[list[dt.date | None]]) -> str:
        slots = [" "] * (len(columns) * CELL_WIDTH)
        for index, name in month_labels(columns):
            start = index * CELL_WIDTH
            if start and slots[start - 1] != " ":
                continue  # would collide with the previous label
            for offset, char in enumerate(name):
                if start + offset < len(slots):
                    slots[start + offset] = char
        return "".join(slots)
