"""Core dataclasses and their JSON (de)serialisation.

No Textual imports here, or in any other engine module.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

MAX_MINUTES = 24 * 60
SKILL_ID_RE = re.compile(r"^[a-z0-9-]+$")
PAYLOAD_KEYS = ("skill", "date", "minutes", "note")
# `ts` alone cannot order an add/edit/del written in the same second: they
# share an id and a device, so the spec's (ts, id, device) tie-break leaves
# them equal. Ranking the op kind makes replay total and deterministic.
OP_RANK = {"add": 0, "edit": 1, "del": 2}


class InvalidOp(ValueError):
    """A log line was structurally unusable and must be counted as unreadable."""


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    created: str
    archived: bool = False
    aliases: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        skill_id = str(data["id"]).strip().lower()
        if not SKILL_ID_RE.match(skill_id):
            raise ValueError(f'invalid skill id "{skill_id}" - use lowercase letters, digits and dashes')
        aliases = tuple(str(a).strip().lower() for a in data.get("aliases") or ())
        return cls(
            id=skill_id,
            name=str(data.get("name") or skill_id),
            created=str(data.get("created") or dt.date.today().isoformat()),
            archived=bool(data.get("archived", False)),
            aliases=aliases,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "archived": self.archived,
            "aliases": list(self.aliases),
        }


@dataclass
class Session:
    """A live practice record, reconstructed by replaying the op log."""

    id: str
    skill: str
    date: dt.date
    minutes: int
    note: str = ""
    ts: str = ""

    @property
    def hours(self) -> float:
        return self.minutes / 60.0


@dataclass(frozen=True)
class Op:
    """One line of a session log.

    `fields` holds only the payload keys present on the line, so an `edit`
    can be merged field-by-field without clobbering absent values. `device`
    comes from the filename, not the line, and only breaks replay ties.
    """

    op: str
    id: str
    ts: str
    fields: dict[str, Any] = field(default_factory=dict)
    device: str = ""

    @property
    def sort_key(self) -> tuple[str, int, str, str]:
        return (self.ts, OP_RANK.get(self.op, 3), self.id, self.device)

    @classmethod
    def from_dict(cls, data: Any, device: str = "") -> "Op":
        if not isinstance(data, dict):
            raise InvalidOp("line is not a JSON object")
        name = data.get("op")
        if not isinstance(name, str) or not name:
            raise InvalidOp("missing op")
        op_id = data.get("id")
        if not isinstance(op_id, str) or not op_id:
            raise InvalidOp("missing id")
        ts = data.get("ts")
        if not isinstance(ts, str) or not ts:
            raise InvalidOp("missing ts")
        payload = {k: _clean(k, data[k]) for k in PAYLOAD_KEYS if k in data}
        if name == "add":
            for required in ("skill", "date", "minutes"):
                if required not in payload:
                    raise InvalidOp(f"add op missing {required}")
        return cls(op=name, id=op_id, ts=ts, fields=payload, device=device)

    def to_line(self) -> str:
        data: dict[str, Any] = {"op": self.op, "id": self.id}
        for key in PAYLOAD_KEYS:
            if key in self.fields:
                value = self.fields[key]
                data[key] = value.isoformat() if isinstance(value, dt.date) else value
        data["ts"] = self.ts
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"

    def with_device(self, device: str) -> "Op":
        return replace(self, device=device)


def _clean(key: str, value: Any) -> Any:
    """Coerce and validate one payload field, or raise InvalidOp."""
    if key == "skill":
        if not isinstance(value, str) or not value.strip():
            raise InvalidOp("skill must be a non-empty string")
        return value.strip().lower()
    if key == "date":
        if not isinstance(value, str):
            raise InvalidOp("date must be a YYYY-MM-DD string")
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidOp(f"bad date {value!r}") from exc
    if key == "minutes":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidOp("minutes must be an integer")
        if value <= 0:
            raise InvalidOp("minutes must be greater than 0")
        return value
    if key == "note":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise InvalidOp("note must be a string")
        return value
    return value


def utc_now() -> str:
    """RFC3339 UTC timestamp, second resolution, as written to the log."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
