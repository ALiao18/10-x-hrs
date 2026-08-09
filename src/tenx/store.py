"""Reading, appending and replaying the append-only op log.

Two invariants live here and nowhere else:

* every session log file ends with a trailing newline (``merge=union``
  corrupts lines otherwise), and
* replay is a pure function of the set of ops, independent of file read
  order, because every op is ordered by ``(ts, op kind, id, device)``. Writes that
  order only by that tie-break, rather than by intent, are reported as
  collisions instead of being resolved silently.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ids import new_ulid
from .models import PAYLOAD_KEYS, Collision, InvalidOp, Op, Party, Session, Skill, utc_now


class StoreError(Exception):
    """The on-disk data is unusable and must not be silently overwritten."""


@dataclass
class LoadResult:
    sessions: dict[str, Session]
    skills: list[Skill]
    unreadable: int = 0
    skipped_ops: int = 0
    collisions: list[Collision] = field(default_factory=list)

    @property
    def skill_ids(self) -> set[str]:
        return {s.id for s in self.skills}


def sessions_dir(root: Path) -> Path:
    return root / "sessions"


def log_path(root: Path, device_id: str) -> Path:
    return sessions_dir(root) / f"{device_id}.jsonl"


def skills_path(root: Path) -> Path:
    return root / "skills.json"


# --- append -----------------------------------------------------------------


def ends_with_newline(path: Path) -> bool:
    """True when it is safe to append directly. Missing/empty counts as safe."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return True
    if size == 0:
        return True
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def append_op(path: Path, op: Op) -> None:
    """Append one complete line. Repairs a missing trailing newline first.

    Unbuffered "ab" is a raw FileIO: exactly one write() syscall, and O_APPEND
    makes seek-to-end plus write atomic against other appenders. The newline
    check uses a separate read handle so this one never seeks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if ends_with_newline(path) else "\n"
    payload = (prefix + op.to_line()).encode("utf-8")
    with open(path, "ab", buffering=0) as fh:
        fh.write(payload)


def make_add(
    skill: str,
    when: dt.date,
    minutes: int,
    note: str = "",
    ts: str | None = None,
) -> Op:
    """Mint an add op. Parsing stays pure by leaving the ULID and clock here."""
    return Op(
        op="add",
        id=new_ulid(),
        ts=ts or utc_now(),
        fields={"skill": skill, "date": when, "minutes": minutes, "note": note},
    )


def commit_message(op: Op) -> str:
    """The one-line git subject for an op, shared by the TUI and `tenx add`."""
    if op.op == "add":
        hours, mins = divmod(op.fields["minutes"], 60)
        length = f"{hours}h{mins:02d}" if hours and mins else (f"{hours}h" if hours else f"{mins}m")
        return f"log: {op.fields['skill']} {length} {op.fields['date'].isoformat()}"
    return f"log: {op.op} {op.id}"


def make_edit(op_id: str, ts: str | None = None, **changed: Any) -> Op:
    return Op(op="edit", id=op_id, ts=ts or utc_now(), fields=dict(changed))


def make_del(op_id: str, ts: str | None = None) -> Op:
    return Op(op="del", id=op_id, ts=ts or utc_now())


def ensure_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    elif not ends_with_newline(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n")


# --- read -------------------------------------------------------------------


def read_ops(root: Path) -> tuple[list[Op], int]:
    """Read every device log. Returns (ops, unreadable line count)."""
    directory = sessions_dir(root)
    ops: list[Op] = []
    unreadable = 0
    if not directory.is_dir():
        return ops, unreadable
    for path in sorted(directory.glob("*.jsonl")):
        device = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ops.append(Op.from_dict(json.loads(line), device))
            except (json.JSONDecodeError, InvalidOp, ValueError):
                unreadable += 1
    return ops, unreadable


@dataclass
class _Record:
    fields: dict[str, Any] = field(default_factory=dict)
    keys: dict[str, tuple] = field(default_factory=dict)

    def merge(self, op: Op) -> list[tuple[str, tuple, Any]]:
        """Last-writer-wins per field, ordered by the op's full sort key.

        Returns the writes it discarded as (field, losing sort key, losing
        value), so the caller can decide which of them were real disagreements.
        """
        displaced: list[tuple[str, tuple, Any]] = []
        for name in PAYLOAD_KEYS:
            if name not in op.fields:
                continue
            if name not in self.keys:
                self.fields[name] = op.fields[name]
                self.keys[name] = op.sort_key
            elif op.sort_key > self.keys[name]:
                displaced.append((name, self.keys[name], self.fields[name]))
                self.fields[name] = op.fields[name]
                self.keys[name] = op.sort_key
            else:
                # A buffered edit that lost: the op itself is the discarded one.
                displaced.append((name, op.sort_key, op.fields[name]))
        return displaced


def replay(ops: Iterable[Op]) -> tuple[dict[str, Session], int, list[Collision]]:
    """Fold ops into live sessions.

    Returns (sessions, skipped unknown ops, unresolved collisions).
    """
    ordered = sorted(ops, key=lambda o: o.sort_key)
    records: dict[str, _Record] = {}
    latest_ts: dict[str, str] = {}
    dead: dict[str, Op] = {}  # id -> the tombstone that killed it
    graveyard: dict[str, dict[str, Any]] = {}  # last payload before a delete
    clashes: dict[tuple[str, str], Collision] = {}
    pending: list[Op] = []
    skipped = 0

    def note_clash(op: Op, displaced: list[tuple[str, tuple, Any]]) -> None:
        record = records[op.id]
        for name in op.fields:
            if record.keys.get(name) == op.sort_key:
                # A write that won on its timestamp settles the field: someone
                # decided this after seeing the state, so there is nothing left
                # for the tie-break to have guessed at.
                clashes.pop((op.id, name), None)
        for name, losing_key, losing_value in displaced:
            winning_key = record.keys[name]
            winning_value = record.fields[name]
            if losing_key[0] != winning_key[0]:
                continue  # the timestamps decided it, which is ordinary LWW
            if losing_key[3] == winning_key[3] or losing_value == winning_value:
                continue  # same machine, or no actual disagreement
            clashes[(op.id, name)] = Collision(
                kind="field",
                session_id=op.id,
                skill=str(record.fields.get("skill", "")),
                date=record.fields.get("date"),
                field_name=name,
                winner=Party(device=winning_key[3], value=winning_value, ts=winning_key[0]),
                loser=Party(device=losing_key[3], value=losing_value, ts=losing_key[0]),
            )

    for op in ordered:
        if op.op == "del":
            if op.id not in dead:
                dead[op.id] = op
                graveyard[op.id] = dict(records[op.id].fields) if op.id in records else {}
                records.pop(op.id, None)
                latest_ts.pop(op.id, None)
            standing = clashes.get((op.id, ""))
            if standing is not None and standing.loser is not None and op.ts > standing.loser.ts:
                clashes.pop((op.id, ""))  # a later delete confirms the deletion
            continue
        if op.id in dead:
            # A write that landed after someone else's tombstone. The tombstone
            # wins by rule rather than by recency, so the newer intent is
            # discarded - surface that instead of dropping it silently.
            tomb = dead[op.id]
            if op.device != tomb.device and op.op in ("add", "edit") and op.ts > tomb.ts:
                known = {**graveyard.get(op.id, {}), **op.fields}
                clashes[(op.id, "")] = Collision(
                    kind="deleted",
                    session_id=op.id,
                    skill=str(known.get("skill", "")),
                    date=known.get("date"),
                    winner=Party(device=tomb.device, value="deleted", ts=tomb.ts),
                    loser=Party(device=op.device, value=op.op, ts=op.ts),
                    known=known,
                )
            continue
        if op.op == "add":
            record = records.setdefault(op.id, _Record())
            note_clash(op, record.merge(op))
            latest_ts[op.id] = max(latest_ts.get(op.id, ""), op.ts)
        elif op.op == "edit":
            if op.id in records:
                note_clash(op, records[op.id].merge(op))
                latest_ts[op.id] = max(latest_ts.get(op.id, ""), op.ts)
            else:
                pending.append(op)
        else:
            skipped += 1

    for op in pending:
        if op.id in records and op.id not in dead:
            note_clash(op, records[op.id].merge(op))
            latest_ts[op.id] = max(latest_ts.get(op.id, ""), op.ts)

    collisions = [clash for _, clash in sorted(clashes.items())]

    sessions = {
        op_id: Session(
            id=op_id,
            skill=record.fields["skill"],
            date=record.fields["date"],
            minutes=record.fields["minutes"],
            note=record.fields.get("note", ""),
            ts=latest_ts.get(op_id, ""),
        )
        for op_id, record in records.items()
    }
    return sessions, skipped, collisions


# --- skills -----------------------------------------------------------------


def load_skills(root: Path) -> list[Skill]:
    path = skills_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{path} must contain a JSON object")
    skills = []
    for entry in data.get("skills") or []:
        try:
            skills.append(Skill.from_dict(entry))
        except (ValueError, KeyError, TypeError):
            continue
    return skills


def save_skills(root: Path, skills: list[Skill]) -> None:
    path = skills_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "skills": [s.to_dict() for s in skills]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load(root: Path) -> LoadResult:
    ops, unreadable = read_ops(root)
    sessions, skipped, collisions = replay(ops)
    return LoadResult(
        sessions=sessions,
        skills=load_skills(root),
        unreadable=unreadable,
        skipped_ops=skipped,
        collisions=collisions,
    )


def export_csv(sessions: Iterable[Session], path: Path) -> int:
    rows = sorted(sessions, key=lambda s: (s.date, s.id))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "skill", "minutes", "note", "id"])
        for s in rows:
            writer.writerow([s.date.isoformat(), s.skill, s.minutes, s.note, s.id])
    return len(rows)
