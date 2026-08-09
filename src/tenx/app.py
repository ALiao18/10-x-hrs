"""The Textual app: layout, command dispatch, and the bridge to the sync thread."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from . import store
from .config import Config, data_dir, default_device_id, load_config
from .models import SKILL_ID_RE, Skill
from .parse import (
    Command,
    ParseError,
    QuickAdd,
    format_day,
    format_duration,
    parse,
    parse_duration,
    resolve_skill,
)
from .stats import Aggregate, aggregate, current_streak, level_label, longest_streak, recent_sessions
from .store import LoadResult, StoreError
from .sync import Status, Syncer, status_label
from .widgets import CommandBar, DetailPanel, Heatmap, SkillTable

DETAIL_LIMIT = 20


def _tail(rest: str) -> str:
    """Everything after the first argument, kept as free text (display names)."""
    _, _, remainder = rest.partition(" ")
    return remainder.strip()


class TenxApp(App):
    CSS_PATH = "tenx.tcss"
    TITLE = "10^x hours"
    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", show=False),
        Binding("escape", "close_detail", "close detail", show=False),
    ]

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = Path(root) if root is not None else data_dir()
        self.config = load_config(self.root) or Config(device_id=default_device_id())
        self.today = dt.date.today()
        self.year = self.today.year
        self.filter_skill: str | None = None
        self.detail_skill: str | None = None
        self.last_list: list[str] = []  # what <n> indexes into
        self.undo_stack: list[str] = []  # adds made since launch, newest last
        self.status_text = status_label(Status.LOCAL_ONLY, 0)
        self.data = LoadResult(sessions={}, skills=[])
        self.agg = Aggregate()
        self.syncer = Syncer(self.root, float(self.config.push_debounce_seconds), self._status_from_thread)

    # -- wiring --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield SkillTable(id="skills")
        yield Heatmap(id="heatmap")
        yield DetailPanel(id="detail")
        yield CommandBar(id="bar")

    def on_mount(self) -> None:
        self.query_one(DetailPanel).display = False
        self.reload()
        self.query_one(Input).focus()
        if self.config.auto_sync:
            self.syncer.start()
            self.syncer.request_pull()

    def on_unmount(self) -> None:
        self.syncer.stop()  # forces the final push

    @property
    def bar(self) -> CommandBar:
        return self.query_one(CommandBar)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value
        result = parse(text, self.data.skills, self.today)
        with self.batch_update():  # table and today's cell change in one frame
            if isinstance(result, ParseError):
                self.bar.fail(result.message)
            elif isinstance(result, QuickAdd):
                self._add(result)
            else:
                self._command(result)

    def action_close_detail(self) -> None:
        self.detail_skill = None
        self.query_one(DetailPanel).display = False

    # -- data ----------------------------------------------------------------

    def reload(self) -> None:
        try:
            self.data = store.load(self.root)
        except StoreError as error:
            self.bar.fail(str(error))
            return
        self.agg = aggregate(self.data.sessions, self.data.skills)
        self.refresh_views()

    def refresh_views(self) -> None:
        self.query_one(SkillTable).update_rows(self.data.skills, self.agg, self.today)
        daily = self.agg.daily_by_skill.get(self.filter_skill, {}) if self.filter_skill else self.agg.daily
        self.query_one(Heatmap).update_data(daily, self.agg.thresholds, self.year)
        self.query_one("#header", Static).update(self._header())
        if self.detail_skill:
            self._render_detail(self.detail_skill)

    def _header(self) -> str:
        total = sum(self.agg.minutes_by_skill.values()) / 60
        live = len([s for s in self.data.skills if not s.archived]) + len(self.agg.unknown_skills)
        days = set(self.agg.daily)
        parts = [
            self.status_text,
            f"{total:,.1f}h across {live} skill{'' if live == 1 else 's'}",
            f"streak {current_streak(days, self.today)}d (longest {longest_streak(days)}d)",
            f"{self.year}" + (f" · {self.filter_skill}" if self.filter_skill else ""),
        ]
        if self.data.unreadable:
            parts.append(f"⚠ {self.data.unreadable} unreadable lines")
        return "  ·  ".join(parts)

    def _display_name(self, skill_id: str) -> str:
        return next((s.name for s in self.data.skills if s.id == skill_id), f"⟨{skill_id}⟩")

    # -- quick add -----------------------------------------------------------

    def _add(self, intent: QuickAdd) -> None:
        op = store.make_add(intent.skill, intent.date, intent.minutes, intent.note)
        store.append_op(store.log_path(self.root, self.config.device_id), op)
        self.undo_stack.append(op.id)
        self.last_list = [op.id]
        self.reload()
        total = self.agg.minutes_by_skill.get(intent.skill, 0) / 60
        self.bar.ok(
            f"{self._display_name(intent.skill)} +{format_duration(intent.minutes)}"
            f" · {format_day(intent.date, self.today)} · {total:,.1f}h total"
        )
        self.syncer.note_write(store.commit_message(op))

    # -- commands ------------------------------------------------------------

    def _command(self, command: Command) -> None:
        handler = getattr(self, f"_cmd_{command.name}", None)
        if handler is None:
            self.bar.fail(f'"{command.name}" is not wired up yet')
            return
        handler(command)

    def _cmd_new(self, command: Command) -> None:
        skill_id = command.args[0].lower()
        if not SKILL_ID_RE.match(skill_id):
            self.bar.fail(f'"{skill_id}" must be lowercase letters, digits and dashes')
            return
        if any(skill.id == skill_id for skill in self.data.skills):
            self.bar.fail(f'"{skill_id}" already exists')
            return
        name = _tail(command.rest) or skill_id
        self._save_skills(self.data.skills + [Skill(skill_id, name, self.today.isoformat())])
        self.bar.ok(f"new skill {name} ({skill_id})")

    def _cmd_rename(self, command: Command) -> None:
        name = _tail(command.rest)
        self._change_skill(command.args[0], f"renamed to {name}", name=name)

    def _cmd_archive(self, command: Command) -> None:
        self._change_skill(command.args[0], "archived", archived=True)

    def _cmd_unarchive(self, command: Command) -> None:
        self._change_skill(command.args[0], "unarchived", archived=False)

    def _cmd_rm(self, command: Command) -> None:
        session = self._find_session(command.args[0])
        if session is None:
            return
        self._append(store.make_del(session.id))
        self.bar.ok(
            f"removed {self._display_name(session.skill)} {format_duration(session.minutes)}"
            f" · {format_day(session.date, self.today)}"
        )

    def _cmd_edit(self, command: Command) -> None:
        session = self._find_session(command.args[0])
        if session is None:
            return
        minutes, error = parse_duration(command.args[1])
        if error or minutes is None:
            self.bar.fail(error or "bad duration")
            return
        self._append(store.make_edit(session.id, minutes=minutes))
        self.bar.ok(f"{self._display_name(session.skill)} is now {format_duration(minutes)}")

    def _cmd_undo(self, command: Command) -> None:
        if not self.undo_stack:
            self.bar.fail("nothing to undo - the stack only covers adds made since launch")
            return
        op_id = self.undo_stack.pop()
        session = self.data.sessions.get(op_id)
        self._append(store.make_del(op_id))
        label = (
            f"{self._display_name(session.skill)} {format_duration(session.minutes)}"
            if session
            else "last entry"
        )
        self.bar.ok(f"undid {label}")

    def _cmd_filter(self, command: Command) -> None:
        if command.args[0].lower() == "off":
            self.filter_skill = None
            self.refresh_views()
            self.bar.ok("heatmap: all skills")
            return
        skill_id, error = resolve_skill(command.args[0], self.data.skills)
        if error or skill_id is None:
            self.bar.fail(error or "unknown skill")
            return
        self.filter_skill = skill_id
        self.refresh_views()
        self.bar.ok(f"heatmap: {self._display_name(skill_id)} only")

    def _cmd_year(self, command: Command) -> None:
        self.year = int(command.args[0])
        self.refresh_views()
        self.bar.ok(f"heatmap: {self.year}")

    def _cmd_detail(self, command: Command) -> None:
        skill_id, error = resolve_skill(command.args[0], self.data.skills)
        if error or skill_id is None:
            self.bar.fail(error or "unknown skill")
            return
        self.detail_skill = skill_id
        self.query_one(DetailPanel).display = True
        self._render_detail(skill_id)
        self.bar.ok(f"{self._display_name(skill_id)} - :rm <n> or :edit <n> <duration>, escape to close")

    def _cmd_sync(self, command: Command) -> None:
        self.syncer.request_pull()
        self.syncer.push_now()
        self.bar.ok("syncing")

    def _cmd_export(self, command: Command) -> None:
        target = (
            Path(command.args[1]).expanduser() if len(command.args) > 1 else self.root / "tenx-export.csv"
        )
        try:
            count = store.export_csv(self.data.sessions.values(), target)
        except OSError as error:
            self.bar.fail(f"could not write {target}: {error}")
            return
        self.bar.ok(f"exported {count} sessions to {target}")

    def _cmd_q(self, command: Command) -> None:
        self.exit()

    # -- shared helpers ------------------------------------------------------

    def _append(self, op) -> None:
        store.append_op(store.log_path(self.root, self.config.device_id), op)
        self.reload()
        self.syncer.note_write(store.commit_message(op))

    def _save_skills(self, skills: list[Skill]) -> None:
        store.save_skills(self.root, skills)
        self.reload()
        self.syncer.note_write("skills: update")

    def _change_skill(self, token: str, done: str, **changes) -> None:
        skill_id, error = resolve_skill(token, self.data.skills)
        if error or skill_id is None:
            self.bar.fail(error or "unknown skill")
            return
        if "name" in changes and not changes["name"]:
            self.bar.fail("give the skill a new display name")
            return
        updated = [
            dataclasses.replace(skill, **changes) if skill.id == skill_id else skill
            for skill in self.data.skills
        ]
        self._save_skills(updated)
        self.bar.ok(f"{skill_id} {done}")

    def _find_session(self, token: str):
        """`<n>` indexes the list last displayed; a ULID (or its prefix) also works."""
        if token.isdigit():
            index = int(token) - 1
            if not 0 <= index < len(self.last_list):
                self.bar.fail(f"no #{token} in the list - open one with `d <skill>` first")
                return None
            session = self.data.sessions.get(self.last_list[index])
        else:
            matches = [key for key in self.data.sessions if key.startswith(token.upper())]
            session = self.data.sessions.get(matches[0]) if len(matches) == 1 else None
        if session is None:
            self.bar.fail(f'no session matching "{token}"')
        return session

    def _render_detail(self, skill_id: str) -> None:
        sessions = recent_sessions(self.data.sessions, skill_id, limit=DETAIL_LIMIT)
        self.last_list = [session.id for session in sessions]
        hours = self.agg.minutes_by_skill.get(skill_id, 0) / 60
        days = set(self.agg.daily_by_skill.get(skill_id, {}))
        heading = "  ·  ".join(
            [
                self._display_name(skill_id),
                f"{hours:,.1f}h",
                level_label(hours),
                f"streak {current_streak(days, self.today)}d (longest {longest_streak(days)}d)",
                f"{self.agg.count_by_skill.get(skill_id, 0)} sessions",
            ]
        )
        self.query_one(DetailPanel).update_data(heading, sessions, self.today)

    # -- sync thread bridge --------------------------------------------------

    def _status_from_thread(self, status: Status, ahead: int) -> None:
        try:
            self.call_from_thread(self._apply_status, status, ahead)
        except Exception:
            pass  # app not running yet, or already torn down

    def _apply_status(self, status: Status, ahead: int) -> None:
        self.status_text = status_label(status, ahead)
        if status is Status.SYNCING:
            self.query_one("#header", Static).update(self._header())
        else:
            self.reload()  # a pull may have brought in another machine's sessions
