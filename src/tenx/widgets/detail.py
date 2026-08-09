"""Per-skill panel: the numbered list that `:rm <n>` and `:edit <n>` index into."""

from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual.widgets import Static

from ..models import Session
from ..parse import format_day, format_duration

NOTE_WIDTH = 52


class DetailPanel(Static):
    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.heading = ""
        self.sessions: list[Session] = []
        self.today = dt.date.today()

    def update_data(self, heading: str, sessions: list[Session], today: dt.date) -> None:
        self.heading = heading
        self.sessions = sessions
        self.today = today
        self.refresh()

    def render(self) -> Text:
        text = Text()
        text.append(self.heading + "\n", style="bold")
        if not self.sessions:
            text.append("  no sessions logged yet", style="dim")
            return text
        for index, session in enumerate(self.sessions, start=1):
            note = session.note
            if len(note) > NOTE_WIDTH:
                # Display only - storage keeps the full note.
                note = note[: NOTE_WIDTH - 1] + "…"
            text.append(f"{index:>3}  ", style="dim")
            text.append(f"{format_day(session.date, self.today):<12}")
            text.append(f"{format_duration(session.minutes):>6}  ")
            text.append(note + "\n", style="dim")
        return text
