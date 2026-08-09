"""Append safety and replay determinism - the multi-machine guarantees."""

import datetime as dt
import json
import subprocess
import sys
import textwrap

from tenx.models import Op
from tenx.store import (
    append_op,
    ends_with_newline,
    export_csv,
    load,
    load_skills,
    log_path,
    read_ops,
    replay,
    save_skills,
    sessions_dir,
)

DAY = dt.date(2026, 8, 9)


def add_op(op_id, skill="ml", minutes=60, note="", ts="2026-08-09T10:00:00Z", day=DAY):
    return Op(op="add", id=op_id, ts=ts, fields={"skill": skill, "date": day, "minutes": minutes, "note": note})


def write_lines(root, device, lines):
    path = log_path(root, device)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line if line.endswith("\n") else line + "\n" for line in lines), encoding="utf-8")
    return path


# --- round trip -------------------------------------------------------------


def test_round_trip(tmp_path):
    path = log_path(tmp_path, "mac")
    for i in range(25):
        append_op(path, add_op(f"ID{i:04d}", minutes=30 + i, ts=f"2026-08-09T10:{i:02d}:00Z"))
    sessions, skipped = replay(read_ops(tmp_path)[0])
    assert len(sessions) == 25
    assert skipped == 0
    assert sessions["ID0007"].minutes == 37


def test_add_edit_del_lifecycle(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, add_op("A", minutes=90, ts="2026-08-09T10:00:00Z"))
    append_op(path, Op(op="edit", id="A", ts="2026-08-09T11:00:00Z", fields={"minutes": 105}))
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 105
    assert sessions["A"].skill == "ml"  # untouched fields survive the merge

    append_op(path, Op(op="del", id="A", ts="2026-08-09T12:00:00Z"))
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions == {}


# --- ordering ---------------------------------------------------------------


def test_edit_before_add_in_file_order(tmp_path):
    """File order is irrelevant: ops are globally sorted by ts before folding."""
    write_lines(
        tmp_path,
        "mac",
        [
            Op(op="edit", id="A", ts="2026-08-09T11:00:00Z", fields={"minutes": 105}).to_line(),
            add_op("A", minutes=90, ts="2026-08-09T10:00:00Z").to_line(),
        ],
    )
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 105


def test_edit_older_than_its_add_is_buffered_and_loses(tmp_path):
    """A genuinely orphaned edit (clock skew) is applied after the pass, but
    per-field last-writer-wins keeps it from clobbering the newer add."""
    write_lines(
        tmp_path,
        "mac",
        [
            Op(op="edit", id="A", ts="2026-08-09T09:00:00Z", fields={"minutes": 105, "note": "old"}).to_line(),
            add_op("A", minutes=90, ts="2026-08-09T10:00:00Z").to_line(),
        ],
    )
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 90
    assert sessions["A"].note == ""


def test_orphan_edit_without_an_add_is_dropped(tmp_path):
    write_lines(tmp_path, "mac", [Op(op="edit", id="GHOST", ts="2026-08-09T09:00:00Z", fields={"minutes": 10}).to_line()])
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions == {}


def test_delete_then_edit_stays_deleted(tmp_path):
    write_lines(
        tmp_path,
        "mac",
        [
            add_op("A", ts="2026-08-09T10:00:00Z").to_line(),
            Op(op="del", id="A", ts="2026-08-09T11:00:00Z").to_line(),
            Op(op="edit", id="A", ts="2026-08-09T12:00:00Z", fields={"minutes": 999}).to_line(),
            add_op("A", minutes=5, ts="2026-08-09T13:00:00Z").to_line(),
        ],
    )
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions == {}


def test_duplicate_add_newer_ts_wins(tmp_path):
    write_lines(
        tmp_path,
        "mac",
        [
            add_op("A", minutes=60, ts="2026-08-09T10:00:00Z").to_line(),
            add_op("A", minutes=120, ts="2026-08-09T11:00:00Z").to_line(),
        ],
    )
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert len(sessions) == 1
    assert sessions["A"].minutes == 120


def test_same_second_add_and_delete_is_deterministic(tmp_path):
    """ts alone cannot order these; the op rank in the sort key can."""
    same = "2026-08-09T10:00:00Z"
    ops = [add_op("A", ts=same), Op(op="del", id="A", ts=same)]
    assert replay(ops)[0] == {}
    assert replay(list(reversed(ops)))[0] == {}


def test_read_order_does_not_change_the_result(tmp_path):
    write_lines(tmp_path, "laptop", [add_op("B", skill="lc", ts="2026-08-09T10:00:00Z").to_line()])
    write_lines(
        tmp_path,
        "mac",
        [
            add_op("A", ts="2026-08-09T10:00:00Z").to_line(),
            Op(op="edit", id="B", ts="2026-08-09T12:00:00Z", fields={"minutes": 45}).to_line(),
        ],
    )
    ops = read_ops(tmp_path)[0]
    forward, _ = replay(ops)
    backward, _ = replay(list(reversed(ops)))
    assert forward == backward
    assert forward["B"].minutes == 45


# --- resilience -------------------------------------------------------------


def test_unknown_op_is_skipped_and_counted(tmp_path):
    write_lines(
        tmp_path,
        "mac",
        [
            add_op("A").to_line(),
            json.dumps({"op": "merge", "id": "Z", "ts": "2026-08-09T10:00:00Z"}) + "\n",
        ],
    )
    ops, unreadable = read_ops(tmp_path)
    sessions, skipped = replay(ops)
    assert unreadable == 0  # a future op kind is not corruption
    assert skipped == 1
    assert len(sessions) == 1


def test_unknown_extra_fields_are_ignored(tmp_path):
    line = json.dumps(
        {
            "op": "add",
            "id": "A",
            "skill": "ml",
            "date": "2026-08-09",
            "minutes": 60,
            "ts": "2026-08-09T10:00:00Z",
            "mood": "great",
            "nested": {"a": 1},
        }
    )
    write_lines(tmp_path, "mac", [line])
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 60


def test_malformed_line_mid_file(tmp_path):
    write_lines(
        tmp_path,
        "mac",
        [
            add_op("A").to_line(),
            "{not json at all",
            json.dumps({"op": "add", "id": "B"}),  # missing ts and payload
            json.dumps({"op": "add", "id": "C", "skill": "ml", "date": "nope", "minutes": 60, "ts": "t"}),
            json.dumps({"op": "add", "id": "D", "skill": "ml", "date": "2026-08-09", "minutes": 0, "ts": "t"}),
            add_op("E").to_line(),
        ],
    )
    ops, unreadable = read_ops(tmp_path)
    sessions, _ = replay(ops)
    assert unreadable == 4
    assert set(sessions) == {"A", "E"}


def test_blank_lines_are_skipped_without_counting(tmp_path):
    write_lines(tmp_path, "mac", ["", add_op("A").to_line().rstrip("\n"), "", "   "])
    ops, unreadable = read_ops(tmp_path)
    assert unreadable == 0
    assert len(ops) == 1


def test_empty_and_missing_inputs(tmp_path):
    assert read_ops(tmp_path) == ([], 0)  # no sessions/ dir
    assert load(tmp_path).sessions == {}
    sessions_dir(tmp_path).mkdir()
    assert read_ops(tmp_path) == ([], 0)  # empty sessions/ dir
    log_path(tmp_path, "mac").write_text("")
    assert read_ops(tmp_path) == ([], 0)  # empty file


# --- the trailing-newline invariant -----------------------------------------


def test_append_repairs_a_missing_trailing_newline(tmp_path):
    path = log_path(tmp_path, "mac")
    path.parent.mkdir(parents=True)
    path.write_text(add_op("A").to_line().rstrip("\n"), encoding="utf-8")  # no trailing newline
    assert not ends_with_newline(path)

    append_op(path, add_op("B", ts="2026-08-09T11:00:00Z"))

    text = path.read_text()
    assert text.endswith("\n")
    assert len([line for line in text.splitlines() if line.strip()]) == 2
    ops, unreadable = read_ops(tmp_path)
    assert unreadable == 0
    assert len(ops) == 2


def test_every_append_leaves_a_trailing_newline(tmp_path):
    path = log_path(tmp_path, "mac")
    for i in range(5):
        append_op(path, add_op(f"ID{i}", ts=f"2026-08-09T10:0{i}:00Z"))
        assert ends_with_newline(path)


def test_concurrent_appends_from_two_processes(tmp_path):
    path = log_path(tmp_path, "mac")
    path.parent.mkdir(parents=True)
    path.write_text("")
    script = textwrap.dedent(
        """
        import datetime as dt, sys
        from pathlib import Path
        from tenx.models import Op
        from tenx.store import append_op
        tag, target = sys.argv[1], Path(sys.argv[2])
        for i in range(150):
            append_op(target, Op(op="add", id=f"{tag}{i:04d}", ts="2026-08-09T10:00:00Z",
                                 fields={"skill": "ml", "date": dt.date(2026, 8, 9),
                                         "minutes": 30, "note": "x" * 120}))
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, tag, str(path)], cwd=str(tmp_path))
        for tag in ("A", "B")
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    ops, unreadable = read_ops(tmp_path)
    assert unreadable == 0, "a line was torn or interleaved"
    assert len(ops) == 300
    assert len({op.id for op in ops}) == 300


# --- skills -----------------------------------------------------------------


def test_skills_round_trip(tmp_path):
    from tenx.models import Skill

    save_skills(tmp_path, [Skill("ml", "machine learning", "2026-08-09", aliases=("mlr",))])
    (loaded,) = load_skills(tmp_path)
    assert loaded.id == "ml" and loaded.aliases == ("mlr",)


def test_missing_skills_file(tmp_path):
    assert load_skills(tmp_path) == []


def test_invalid_skill_entries_are_skipped(tmp_path):
    (tmp_path / "skills.json").write_text(
        json.dumps({"version": 1, "skills": [{"id": "ok", "name": "ok"}, {"id": "NOT OK"}, {}]})
    )
    assert [s.id for s in load_skills(tmp_path)] == ["ok"]


def test_export_csv(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, add_op("A", minutes=90, note="sweep"))
    out = tmp_path / "out.csv"
    sessions, _ = replay(read_ops(tmp_path)[0])
    assert export_csv(sessions.values(), out) == 1
    assert "sweep" in out.read_text()
