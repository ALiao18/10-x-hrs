"""The Textual app: layout, command dispatch, and the bridge to the sync thread."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from . import store
from .config import Config, data_dir, default_device_id, load_config, save_config
from .models import EXTRA_PREFIX, SKILL_ID_RE, Collision, Skill
from .parse import (
    Command,
    ParseError,
    QuickAdd,
    format_day,
    format_duration,
    parse,
    parse_duration,
    parse_metric,
    resolve_skill,
)
from .stats import (
    Aggregate,
    aggregate,
    bucket_thresholds,
    current_streak,
    level_label,
    longest_streak,
    metric_totals,
    recent_sessions,
)
from .store import LoadResult, StoreError
from .sync import Status, Syncer, status_label
from .widgets import CommandBar, DetailPanel, Heatmap, SkillTable

DETAIL_LIMIT = 20


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _show(field_name: str, value: Any) -> str:
    field_name = field_name.removeprefix(EXTRA_PREFIX)
    if field_name == "minutes" and isinstance(value, int):
        return format_duration(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return f'"{value}"' if value == "" else str(value)


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
        self.conflict_list: list[Collision] = []  # what :fix <n> indexes into
        self.showing: str | None = None  # which panel the detail area is holding
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
        text = self._apply_default_minutes(event.value)
        result = parse(text, self.data.skills, self.today)
        with self.batch_update():  # table and today's cell change in one frame
            if isinstance(result, ParseError):
                self.bar.fail(result.message)
            elif isinstance(result, QuickAdd):
                self._add(result)
            else:
                self._command(result)
            self._nag_about_conflicts()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.bar.set_hint(self._metric_hint(event.value))

    def _metric_hint(self, text: str) -> str:
        """Undeclared-in-this-line metric keys for whatever skill is typed so
        far, so "lift 45m ▸ bodypart=" is discoverable without `d <skill>`."""
        tokens = text.split()
        if not tokens or tokens[0].startswith(":"):
            return ""
        skill_id, error = resolve_skill(tokens[0], self.data.skills)
        if error or skill_id is None:
            return ""
        declared = next((s.metrics for s in self.data.skills if s.id == skill_id), ())
        missing = [key for key in declared if not any(t.lower().startswith(f"{key}=") for t in tokens)]
        if not missing:
            return ""
        return "▸ " + " ".join(f"{key}=" for key in missing)

    def _apply_default_minutes(self, text: str) -> str:
        """A bare skill name ("train") logs `default_minutes` if one is set -
        the parser stays pure, so this rewrites the input before it gets there."""
        if self.config.default_minutes is None:
            return text
        stripped = text.strip()
        if not stripped or " " in stripped or stripped.startswith(":"):
            return text
        if parse_duration(stripped)[0] is not None:
            return text  # already a bare duration, not a skill name
        return f"{stripped} {self.config.default_minutes}m"

    def _nag_about_conflicts(self) -> None:
        """Raise unresolved conflicts on the entry itself.

        Appended to the echo rather than blocking: a prompt here would cost a
        keystroke on every log, and entry speed is the product.
        """
        count = len(self.data.collisions)
        if count and self.showing != "conflicts" and self.bar.last_message.startswith("✓"):
            self.bar.note(f"⚠ {_plural(count, 'conflict')} - :conflicts")

    def action_close_detail(self) -> None:
        self.detail_skill = None
        self.showing = None
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
        self.query_one(Heatmap).update_data(daily, bucket_thresholds(daily), self.year)
        self.query_one("#header", Static).update(self._header())
        self.bar.set_today(self._today_line())
        if self.showing == "skill" and self.detail_skill:
            self._render_detail(self.detail_skill)
        elif self.showing == "conflicts":
            self._render_conflicts()

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
        if self.data.skipped_ops:
            parts.append(f"⚠ {_plural(self.data.skipped_ops, 'orphan edit')}")
        if self.data.collisions:
            parts.append(f"⚠ {_plural(len(self.data.collisions), 'conflict')} · :conflicts")
        return "  ·  ".join(parts)

    def _today_line(self) -> str:
        per_skill = {
            skill: minutes
            for skill, daily in self.agg.daily_by_skill.items()
            if (minutes := daily.get(self.today, 0))
        }
        if not per_skill:
            return "today · nothing logged yet"
        ordered = sorted(per_skill.items(), key=lambda kv: -kv[1])
        bits = " · ".join(f"{self._display_name(skill)} {format_duration(minutes)}" for skill, minutes in ordered)
        return f"today · {bits} = {format_duration(sum(per_skill.values()))}"

    def _display_name(self, skill_id: str) -> str:
        return next((s.name for s in self.data.skills if s.id == skill_id), f"⟨{skill_id}⟩")

    # -- quick add -----------------------------------------------------------

    def _add(self, intent: QuickAdd) -> None:
        op = store.make_add(intent.skill, intent.date, intent.minutes, intent.note, extra=intent.metrics)
        store.append_op(store.log_path(self.root, self.config.device_id), op)
        self.undo_stack.append(op.id)
        self.last_list = [op.id]
        self.reload()
        total = self.agg.minutes_by_skill.get(intent.skill, 0) / 60
        metric_bits = " · ".join(f"{key} {value}" for key, value in intent.metrics.items())
        self.bar.ok(
            f"{self._display_name(intent.skill)} +{format_duration(intent.minutes)}"
            + (f" · {metric_bits}" if metric_bits else "")
            + f" · {format_day(intent.date, self.today)} · {total:,.1f}h total"
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
        declared = next((s.metrics for s in self.data.skills if s.id == session.skill), ())
        metric, error = parse_metric(command.args[1], declared)
        if error:
            self.bar.fail(error)
            return
        if metric is not None:
            key, value = metric
            self._append(store.make_edit(session.id, **{EXTRA_PREFIX + key: value}))
            self.bar.ok(f"{self._display_name(session.skill)} {key} is now {value}")
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
        self.showing = "skill"
        self.query_one(DetailPanel).display = True
        self._render_detail(skill_id)
        self.bar.ok(f"{self._display_name(skill_id)} - :rm <n> or :edit <n> <duration>, escape to close")

    def _cmd_metric(self, command: Command) -> None:
        """Declare (or undeclare) a custom metric for one skill."""
        skill_id, error = resolve_skill(command.args[0], self.data.skills)
        if error or skill_id is None:
            self.bar.fail(error or "unknown skill")
            return
        raw = command.args[1].lower()
        dropping = raw.startswith("-")
        key = raw.lstrip("-")
        skill = next(s for s in self.data.skills if s.id == skill_id)
        declared = set(skill.metrics)
        if dropping and key not in declared:
            self.bar.fail(f'{skill_id} has no metric "{key}"')
            return
        declared.discard(key) if dropping else declared.add(key)
        self._save_skills(
            [
                dataclasses.replace(s, metrics=tuple(sorted(declared))) if s.id == skill_id else s
                for s in self.data.skills
            ]
        )
        if dropping:
            self.bar.ok(f"{skill_id} no longer records {key} (logged values are kept)")
        else:
            self.bar.ok(f'{skill_id} now records {key} - log it with "{skill_id} 45m {key}=..."')

    def _cmd_sync(self, command: Command) -> None:
        self.syncer.request_pull()
        self.syncer.push_now()
        self.bar.ok("syncing")

    def _cmd_export(self, command: Command) -> None:
        target = (
            Path(command.args[1]).expanduser() if len(command.args) > 1 else self.root / "tenx-export.csv"
        )
        try:
            metrics = {key for skill in self.data.skills for key in skill.metrics}
            count = store.export_csv(self.data.sessions.values(), target, metrics)
        except OSError as error:
            self.bar.fail(f"could not write {target}: {error}")
            return
        self.bar.ok(f"exported {count} sessions to {target}")

    def _cmd_conflicts(self, command: Command) -> None:
        self.showing = "conflicts"
        self.detail_skill = None
        self.query_one(DetailPanel).display = True
        self._render_conflicts()
        if self.conflict_list:
            self.bar.ok("settle one with :fix <n> <action>")
        else:
            self.bar.ok("no conflicts - every machine agrees")

    def _cmd_fix(self, command: Command) -> None:
        index = int(command.args[0]) - 1
        action = command.args[1].lower()
        if not 0 <= index < len(self.conflict_list):
            self.bar.fail("run :conflicts first, then :fix <n> <action>")
            return
        clash = self.conflict_list[index]
        if clash.kind == "field":
            self._fix_field(clash, action)
        else:
            self._fix_deleted(clash, action)

    def _fix_field(self, clash: Collision, action: str) -> None:
        if action not in ("mine", "theirs"):
            self.bar.fail(f'"{action}" settles a deletion, not a value - use mine/theirs')
            return
        value, error = self._pick_side(clash, action)
        if error:
            self.bar.fail(error)
            return
        self._append(store.make_edit(clash.session_id, **{clash.field_name: value}))
        self.bar.ok(f"settled {clash.field_name} = {_show(clash.field_name, value)}")

    def _fix_deleted(self, clash: Collision, action: str) -> None:
        if action == "drop":
            self._append(store.make_del(clash.session_id))
            self.bar.ok("kept it deleted")
            return
        if action != "keep":
            self.bar.fail(f'"{action}" settles a value, not a deletion - use keep or drop')
            return
        known = clash.known
        if not all(key in known for key in ("skill", "date", "minutes")):
            self.bar.fail("too little of that session survived to restore it - use drop")
            return
        # Tombstones are absorbing, so restoring means re-adding under a fresh
        # id. The original stays dead, settled so it stops being reported.
        restored = store.make_add(known["skill"], known["date"], known["minutes"], known.get("note", ""))
        self._append(restored, store.make_del(clash.session_id))
        self.bar.ok(f"restored {self._display_name(known['skill'])} as a new session")

    def _pick_side(self, clash: Collision, action: str) -> tuple[Any, str | None]:
        assert clash.winner is not None and clash.loser is not None
        me = self.config.device_id
        mine = next((side for side in (clash.winner, clash.loser) if side.device == me), None)
        theirs = next((side for side in (clash.winner, clash.loser) if side.device != me), None)
        if mine is None:
            others = f"{clash.winner.device} and {clash.loser.device}"
            return None, f"neither side is this machine ({me}) - settle it from {others} instead"
        chosen = mine if action == "mine" else theirs
        assert chosen is not None
        return chosen.value, None

    def _render_conflicts(self) -> None:
        self.conflict_list = list(self.data.collisions)
        panel = self.query_one(DetailPanel)
        if not self.conflict_list:
            panel.update_lines("no conflicts - every machine agrees", [])
            return
        lines: list[str] = []
        for number, clash in enumerate(self.conflict_list, start=1):
            when = clash.date.isoformat() if clash.date else "unknown date"
            where = f"{self._display_name(clash.skill) if clash.skill else '?'} · {when}"
            assert clash.winner is not None and clash.loser is not None
            if clash.kind == "field":
                lines.append(f"{number:>3}  {where} · {clash.field_name.removeprefix(EXTRA_PREFIX)}")
                for label, side in (("kept", clash.winner), ("lost", clash.loser)):
                    value = _show(clash.field_name, side.value)
                    lines.append(f"     {label}  {side.device:<14}{value:<12}{side.ts}")
                lines.append(f"     :fix {number} mine | theirs")
            else:
                lines.append(f"{number:>3}  {where} · deleted on {clash.winner.device},")
                lines.append(f"     then {clash.loser.value}ed on {clash.loser.device} at {clash.loser.ts}")
                lines.append(f"     :fix {number} keep | drop")
        panel.update_lines(_plural(len(self.conflict_list), "unresolved conflict"), lines)

    def _cmd_default(self, command: Command) -> None:
        raw = command.args[0].lower()
        self.config.default_minutes = None if raw == "off" else int(raw)
        save_config(self.root, self.config)
        if self.config.default_minutes is None:
            self.bar.ok("default duration cleared")
        else:
            self.bar.ok(f"a bare skill name now logs {format_duration(self.config.default_minutes)}")

    def _cmd_q(self, command: Command) -> None:
        self.exit()

    # -- shared helpers ------------------------------------------------------

    def _append(self, *ops) -> None:
        path = store.log_path(self.root, self.config.device_id)
        for op in ops:
            store.append_op(path, op)
        self.reload()
        for op in ops:
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
        parts = [
            self._display_name(skill_id),
            f"{hours:,.1f}h",
            level_label(hours),
            f"streak {current_streak(days, self.today)}d (longest {longest_streak(days)}d)",
            f"{self.agg.count_by_skill.get(skill_id, 0)} sessions",
        ]
        parts += [
            f"{key} {total:,.10g} (avg {mean:,.10g})"
            for key, (total, mean) in sorted(metric_totals(self.data.sessions, skill_id).items())
        ]
        heading = "  ·  ".join(parts)
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
