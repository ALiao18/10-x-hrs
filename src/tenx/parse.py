"""The command-bar grammar.

`parse` is a pure function of ``(text, skills, today)``. It touches no
filesystem, no clock (unless you omit `today`) and no randomness, so it can be
fuzzed freely. Creating the ULID and the timestamp is the caller's job.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .models import MAX_MINUTES, Skill


@dataclass(frozen=True)
class QuickAdd:
    skill: str
    minutes: int
    date: dt.date
    note: str = ""


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    rest: str = ""


@dataclass(frozen=True)
class ParseError:
    message: str


Parsed = QuickAdd | Command | ParseError

# name -> (min args, max args or None, usage shown on a mistake)
COMMANDS: dict[str, tuple[int, int | None, str]] = {
    "new": (1, None, ":new <id> [display name]"),
    "rename": (2, None, ":rename <id> <new name>"),
    "archive": (1, 1, ":archive <id>"),
    "unarchive": (1, 1, ":unarchive <id>"),
    "rm": (1, 1, ":rm <n|id>"),
    "edit": (2, 2, ":edit <n|id> <duration>"),
    "undo": (0, 0, ":undo"),
    "filter": (1, 1, ":filter <id> | :filter off"),
    "year": (1, 1, ":year <YYYY>"),
    "detail": (1, 1, ":detail <id>"),
    "sync": (0, 0, ":sync"),
    "export": (1, 2, ":export csv [path]"),
    "q": (0, 0, ":q"),
}

_DUR_HM = re.compile(r"^(\d+)h(\d+)m?$")
_DUR_H = re.compile(r"^(\d+(?:\.\d+)?)h$")
_DUR_M = re.compile(r"^(\d+)m$")
_DUR_N = re.compile(r"^(\d+)$")

_DATE_MD = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_AGO = re.compile(r"^-(\d+)$")
_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def parse(text: str, skills: list[Skill], today: dt.date | None = None) -> Parsed:
    if today is None:
        today = dt.date.today()
    raw = text.strip()
    if not raw:
        return ParseError("type a skill and a duration, e.g. ml 1h30")
    if raw.startswith(":"):
        return parse_command(raw)
    shortcut = _detail_shortcut(raw, skills)
    if shortcut is not None:
        return shortcut
    return _parse_add(raw, skills, today)


def _detail_shortcut(raw: str, skills: list[Skill]) -> Command | None:
    """`d ml` opens the detail view - the shorthand used in the walkthroughs.

    Only fires when `d` is not itself a skill and the second token is not a
    duration, so a skill called `d` and `d 30m` both keep working.
    """
    tokens = raw.split()
    if len(tokens) != 2 or tokens[0].lower() != "d":
        return None
    if any(skill.id == "d" or "d" in skill.aliases for skill in skills):
        return None
    if parse_duration(tokens[1])[0] is not None:
        return None
    return Command(name="detail", args=(tokens[1],), rest=tokens[1])


def parse_command(raw: str) -> Command | ParseError:
    body = raw.lstrip(":").strip()
    if not body:
        return ParseError("type a command, e.g. :new ml")
    head, _, rest = body.partition(" ")
    name = head.lower()
    if name not in COMMANDS:
        return ParseError(f'unknown command ":{head}"')
    rest = rest.strip()
    args = tuple(rest.split())
    low, high, usage = COMMANDS[name]
    if len(args) < low or (high is not None and len(args) > high):
        return ParseError(f"usage: {usage}")
    if name == "year" and not re.fullmatch(r"\d{4}", args[0]):
        return ParseError(f"usage: {usage}")
    if name == "export" and args[0].lower() != "csv":
        return ParseError(f"usage: {usage}")
    return Command(name=name, args=args, rest=rest)


def _parse_add(raw: str, skills: list[Skill], today: dt.date) -> Parsed:
    tokens = raw.split()
    skill_id, error = resolve_skill(tokens[0], skills)
    if error:
        return ParseError(error)
    if len(tokens) < 2:
        return ParseError(f'how long? try "{tokens[0]} 1h30"')
    minutes, error = parse_duration(tokens[1])
    if error:
        return ParseError(error)
    when = today
    rest = tokens[2:]
    if rest:
        # A date is only consumed from position 3, and only if it really is
        # one - otherwise "ml 1h 5 papers read" would lose its first word.
        candidate, error = parse_date(rest[0], today)
        if error:
            return ParseError(error)
        if candidate is not None:
            when = candidate
            rest = rest[1:]
    assert skill_id is not None and minutes is not None
    return QuickAdd(skill=skill_id, minutes=minutes, date=when, note=" ".join(rest))


def parse_duration(token: str) -> tuple[int | None, str | None]:
    """Return (minutes, error)."""
    text = token.lower()
    if match := _DUR_HM.match(text):
        minutes = int(match.group(1)) * 60 + int(match.group(2))
    elif match := _DUR_H.match(text):
        minutes = round(float(match.group(1)) * 60)
    elif match := _DUR_M.match(text):
        minutes = int(match.group(1))
    elif match := _DUR_N.match(text):
        minutes = int(match.group(1))
    else:
        return None, f'can\'t read "{token}" as a duration - try 1h30'
    if minutes <= 0:
        return None, "a session has to be longer than 0 minutes"
    if minutes > MAX_MINUTES:
        return None, f'"{token}" is over the 24h-per-session limit'
    return minutes, None


def parse_date(token: str, today: dt.date) -> tuple[dt.date | None, str | None]:
    """Return (date, error). (None, None) means the token is not a date at all."""
    text = token.lower()
    if text == "today":
        return today, None
    if text == "yesterday":
        return today - dt.timedelta(days=1), None
    if text in _WEEKDAYS:
        # Most recent occurrence; today counts when the weekday matches.
        return today - dt.timedelta(days=(today.weekday() - _WEEKDAYS[text]) % 7), None
    if match := _DATE_AGO.match(text):
        return today - dt.timedelta(days=int(match.group(1))), None
    if match := _DATE_MD.match(text):
        month, day = int(match.group(1)), int(match.group(2))
        for year in (today.year, today.year - 1):
            try:
                candidate = dt.date(year, month, day)
            except ValueError:
                continue
            if candidate <= today:
                return candidate, None
        return None, f'"{token}" is not a real date'
    if _DATE_ISO.match(text):
        try:
            candidate = dt.date.fromisoformat(text)
        except ValueError:
            return None, f'"{token}" is not a real date'
        if candidate > today:
            return None, f'"{token}" is in the future'
        return candidate, None
    return None, None


def resolve_skill(token: str, skills: list[Skill]) -> tuple[str | None, str | None]:
    """Exact id, then exact alias, then a unique prefix of id/alias/name."""
    text = token.strip().lower()
    if not text:
        return None, "which skill?"
    for skill in skills:
        if skill.id == text:
            return skill.id, None
    for skill in skills:
        if text in skill.aliases:
            return skill.id, None
    matches = {
        skill.id
        for skill in skills
        if skill.id.startswith(text)
        or skill.name.lower().startswith(text)
        or any(alias.startswith(text) for alias in skill.aliases)
    }
    if len(matches) == 1:
        return matches.pop(), None
    if not matches:
        return None, f'unknown skill "{token}" - :new {text}'
    return None, f'"{token}" matches {", ".join(sorted(matches))} - be more specific'


def format_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h{mins:02d}"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def format_day(when: dt.date, today: dt.date) -> str:
    delta = (today - when).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return when.strftime("%a")
    if when.year == today.year:
        return when.strftime("%-d %b")
    return when.strftime("%-d %b %Y")
