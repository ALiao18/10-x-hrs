"""Append safety and replay determinism - the multi-machine guarantees."""

import csv
import datetime as dt
import json
import subprocess
import sys
import textwrap

from tenx.models import Op
from tenx.store import (
    append_op,
    make_add,
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
    return Op(
        op="add", id=op_id, ts=ts, fields={"skill": skill, "date": day, "minutes": minutes, "note": note}
    )


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
    sessions, skipped, _ = replay(read_ops(tmp_path)[0])
    assert len(sessions) == 25
    assert skipped == 0
    assert sessions["ID0007"].minutes == 37


def test_add_edit_del_lifecycle(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, add_op("A", minutes=90, ts="2026-08-09T10:00:00Z"))
    append_op(path, Op(op="edit", id="A", ts="2026-08-09T11:00:00Z", fields={"minutes": 105}))
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 105
    assert sessions["A"].skill == "ml"  # untouched fields survive the merge

    append_op(path, Op(op="del", id="A", ts="2026-08-09T12:00:00Z"))
    sessions, _, _ = replay(read_ops(tmp_path)[0])
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
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 105


def test_edit_older_than_its_add_is_buffered_and_loses(tmp_path):
    """A genuinely orphaned edit (clock skew) is applied after the pass, but
    per-field last-writer-wins keeps it from clobbering the newer add."""
    write_lines(
        tmp_path,
        "mac",
        [
            Op(
                op="edit", id="A", ts="2026-08-09T09:00:00Z", fields={"minutes": 105, "note": "old"}
            ).to_line(),
            add_op("A", minutes=90, ts="2026-08-09T10:00:00Z").to_line(),
        ],
    )
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    assert sessions["A"].minutes == 90
    assert sessions["A"].note == ""


def test_orphan_edit_without_an_add_is_dropped(tmp_path):
    """The exact shape of a half-synced multi-machine state: an edit whose
    add never showed up must not vanish without a trace - it's counted like
    any other unusable op, not silently discarded."""
    write_lines(
        tmp_path,
        "mac",
        [Op(op="edit", id="GHOST", ts="2026-08-09T09:00:00Z", fields={"minutes": 10}).to_line()],
    )
    sessions, skipped, _ = replay(read_ops(tmp_path)[0])
    assert sessions == {}
    assert skipped == 1


def test_orphan_edit_for_an_already_deleted_id_is_also_counted(tmp_path):
    """An edit that arrives for an id that was deleted before it ever had an
    add is just as orphaned, and must be counted too."""
    sessions, skipped, _ = replay(
        [
            Op(op="del", id="GHOST", ts="2026-08-09T09:00:00Z"),
            Op(op="edit", id="GHOST", ts="2026-08-09T08:00:00Z", fields={"minutes": 10}),
        ]
    )
    assert sessions == {}
    assert skipped == 1


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
    sessions, _, _ = replay(read_ops(tmp_path)[0])
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
    sessions, _, _ = replay(read_ops(tmp_path)[0])
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
    forward, _, _ = replay(ops)
    backward, _, _ = replay(list(reversed(ops)))
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
    sessions, skipped, _ = replay(ops)
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
    sessions, _, _ = replay(read_ops(tmp_path)[0])
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
            json.dumps(
                {"op": "add", "id": "D", "skill": "ml", "date": "2026-08-09", "minutes": 0, "ts": "t"}
            ),
            add_op("E").to_line(),
        ],
    )
    ops, unreadable = read_ops(tmp_path)
    sessions, _, _ = replay(ops)
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
    script = textwrap.dedent("""
        import datetime as dt, sys
        from pathlib import Path
        from tenx.models import Op
        from tenx.store import append_op
        tag, target = sys.argv[1], Path(sys.argv[2])
        for i in range(150):
            append_op(target, Op(op="add", id=f"{tag}{i:04d}", ts="2026-08-09T10:00:00Z",
                                 fields={"skill": "ml", "date": dt.date(2026, 8, 9),
                                         "minutes": 30, "note": "x" * 120}))
        """)
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
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    assert export_csv(sessions.values(), out) == 1
    assert "sweep" in out.read_text()


# --- collisions -------------------------------------------------------------


def edit_op(op_id, device, ts, **changed):
    return Op(op="edit", id=op_id, ts=ts, fields=dict(changed), device=device)


def del_op(op_id, device, ts):
    return Op(op="del", id=op_id, ts=ts, device=device)


def base(device="mac", ts="2026-08-09T10:00:00Z", minutes=60):
    op = add_op("A", minutes=minutes, ts=ts)
    return Op(op="add", id="A", ts=ts, fields=op.fields, device=device)


SAME = "2026-08-09T11:00:00Z"


def test_a_same_second_disagreement_is_a_collision():
    """The tie-break, not the clock, picked the winner - so say so."""
    _, _, clashes = replay(
        [
            base(),
            edit_op("A", "mac", SAME, minutes=105),
            edit_op("A", "laptop", SAME, minutes=120),
        ]
    )
    assert len(clashes) == 1
    clash = clashes[0]
    assert clash.kind == "field" and clash.field_name == "minutes"
    assert (clash.winner.device, clash.winner.value) == ("mac", 105)
    assert (clash.loser.device, clash.loser.value) == ("laptop", 120)


def test_an_ordinary_remote_edit_is_not_a_collision():
    """Logged on the mac, edited on the laptop an hour later. The laptop had
    seen the value it was changing - that is the normal flow, not a clash."""
    _, _, clashes = replay([base(minutes=60), edit_op("A", "laptop", "2026-08-09T11:00:00Z", minutes=150)])
    assert clashes == []


def test_one_machine_changing_its_mind_is_not_a_collision():
    _, _, clashes = replay(
        [
            base(),
            edit_op("A", "mac", SAME, minutes=105),
            edit_op("A", "mac", SAME, minutes=120),
        ]
    )
    assert clashes == []


def test_two_machines_agreeing_is_not_a_collision():
    _, _, clashes = replay(
        [
            base(),
            edit_op("A", "mac", SAME, minutes=120),
            edit_op("A", "laptop", SAME, minutes=120),
        ]
    )
    assert clashes == []


def test_editing_what_another_machine_deleted_is_a_collision():
    sessions, _, clashes = replay(
        [
            base(minutes=90),
            del_op("A", "mac", "2026-08-09T11:00:00Z"),
            edit_op("A", "laptop", "2026-08-09T12:00:00Z", minutes=150),
        ]
    )
    assert sessions == {}, "the spec keeps it deleted"
    assert len(clashes) == 1 and clashes[0].kind == "deleted"
    # Enough of the record survives to offer a restore.
    assert clashes[0].known["minutes"] == 150 and clashes[0].known["skill"] == "ml"


def test_deleting_after_someone_elses_edit_is_the_normal_sequence():
    _, _, clashes = replay(
        [
            base(),
            edit_op("A", "laptop", "2026-08-09T11:00:00Z", minutes=150),
            del_op("A", "mac", "2026-08-09T12:00:00Z"),
        ]
    )
    assert clashes == [], "deleting something someone edited earlier is not a disagreement"


def test_a_later_write_settles_a_field_collision():
    """This is all `:fix` does: write the chosen value now. A clearly-timed
    write means nothing is left for the tie-break to have guessed."""
    ops = [base(), edit_op("A", "mac", SAME, minutes=105), edit_op("A", "laptop", SAME, minutes=120)]
    assert len(replay(ops)[2]) == 1
    settled = ops + [edit_op("A", "mac", "2026-08-09T12:00:00Z", minutes=120)]
    sessions, _, clashes = replay(settled)
    assert clashes == []
    assert sessions["A"].minutes == 120


def test_a_later_delete_settles_a_deletion_collision():
    ops = [
        base(),
        del_op("A", "mac", "2026-08-09T11:00:00Z"),
        edit_op("A", "laptop", "2026-08-09T12:00:00Z", minutes=150),
    ]
    assert len(replay(ops)[2]) == 1
    sessions, _, clashes = replay(ops + [del_op("A", "mac", "2026-08-09T13:00:00Z")])
    assert clashes == [] and sessions == {}


def test_a_fresh_disagreement_after_a_resolution_reopens():
    ops = [
        base(),
        edit_op("A", "mac", SAME, minutes=105),
        edit_op("A", "laptop", SAME, minutes=120),
        edit_op("A", "mac", "2026-08-09T12:00:00Z", minutes=105),
        edit_op("A", "mac", "2026-08-09T13:00:00Z", minutes=200),
        edit_op("A", "laptop", "2026-08-09T13:00:00Z", minutes=90),
    ]
    _, _, clashes = replay(ops)
    assert len(clashes) == 1 and {clashes[0].winner.value, clashes[0].loser.value} == {200, 90}


def test_resolving_needs_no_new_op_kind_or_field(tmp_path):
    """A resolution is an ordinary edit, so a machine running older code
    applies it too - nothing about the log format had to change."""
    path = log_path(tmp_path, "mac")
    append_op(path, edit_op("A", "", "2026-08-09T12:00:00Z", minutes=105))
    assert set(json.loads(path.read_text())) == {"op", "id", "minutes", "ts"}


def test_collisions_do_not_depend_on_read_order():
    ops = [
        base(),
        edit_op("A", "mac", SAME, minutes=105),
        edit_op("A", "laptop", SAME, minutes=120),
        del_op("B", "mac", "2026-08-09T11:00:00Z"),
        Op(
            op="add",
            id="B",
            ts="2026-08-09T12:00:00Z",
            device="laptop",
            fields={"skill": "lc", "date": DAY, "minutes": 30, "note": ""},
        ),
    ]
    forward = replay(ops)[2]
    backward = replay(list(reversed(ops)))[2]
    assert forward == backward
    assert {c.kind for c in forward} == {"field", "deleted"}


# --- custom metrics ---------------------------------------------------------


def test_metrics_round_trip_through_the_log(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, make_add("run", DAY, 45, "easy", extra={"distance": 8.2, "shoes": "vaporfly"}))
    line = json.loads(path.read_text())
    assert line["extra"] == {"distance": 8.2, "shoes": "vaporfly"}, "nested, not top level"
    (session,) = replay(read_ops(tmp_path)[0])[0].values()
    assert session.extra == {"distance": 8.2, "shoes": "vaporfly"}
    assert session.minutes == 45 and session.note == "easy"


def test_a_metric_can_be_edited_without_touching_the_rest(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, make_add("run", DAY, 45, "easy", ts="2026-08-09T10:00:00Z", extra={"distance": 8.2}))
    session_id = next(iter(replay(read_ops(tmp_path)[0])[0]))
    append_op(
        path,
        Op(op="edit", id=session_id, ts="2026-08-09T11:00:00Z", fields={"extra:distance": 8.6}),
    )
    (session,) = replay(read_ops(tmp_path)[0])[0].values()
    assert session.extra == {"distance": 8.6}
    assert session.minutes == 45 and session.note == "easy", "untouched fields survive"


def test_metrics_collide_like_any_other_field():
    fields = {"skill": "run", "date": DAY, "minutes": 45, "extra:distance": 8.2}
    _, _, clashes = replay(
        [
            Op("add", "A", "2020-01-01T10:00:00Z", fields, "mac"),
            Op("edit", "A", SAME, {"extra:distance": 8.6}, "mac"),
            Op("edit", "A", SAME, {"extra:distance": 9.1}, "laptop"),
        ]
    )
    assert len(clashes) == 1
    assert clashes[0].field_name == "extra:distance"
    assert {clashes[0].winner.value, clashes[0].loser.value} == {8.6, 9.1}


def test_a_line_with_a_junk_extra_block_still_loads(tmp_path):
    write_lines(
        tmp_path,
        "mac",
        [
            json.dumps(
                {
                    "op": "add",
                    "id": "A",
                    "skill": "run",
                    "date": "2026-08-09",
                    "minutes": 45,
                    "extra": {"distance": 8.2, "BAD KEY": 1, "nested": {"a": 1}, "flag": True},
                    "ts": "2026-08-09T10:00:00Z",
                }
            ),
            json.dumps(
                {
                    "op": "add",
                    "id": "B",
                    "skill": "run",
                    "date": "2026-08-09",
                    "minutes": 30,
                    "extra": "not an object",
                    "ts": "2026-08-09T10:00:00Z",
                }
            ),
        ],
    )
    ops, unreadable = read_ops(tmp_path)
    sessions, _, _ = replay(ops)
    assert unreadable == 0
    assert sessions["A"].extra == {"distance": 8.2}, "only usable scalars with valid keys survive"
    assert sessions["B"].extra == {}


def test_export_gives_every_metric_a_column(tmp_path):
    path = log_path(tmp_path, "mac")
    append_op(path, make_add("run", DAY, 45, "easy", extra={"distance": 8.2}))
    append_op(path, make_add("ml", DAY, 60, "sweep"))
    out = tmp_path / "out.csv"
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    export_csv(sessions.values(), out, ["distance", "elevation"])
    rows = list(csv.reader(out.read_text().splitlines()))
    assert rows[0] == ["date", "skill", "minutes", "note", "id", "distance", "elevation"]
    by_skill = {row[1]: row for row in rows[1:]}
    assert by_skill["run"][5] == "8.2"
    assert by_skill["ml"][5] == "", "a skill without the metric leaves it blank"


def test_export_keeps_columns_for_undeclared_metrics(tmp_path):
    """Undeclaring a metric must not hide values already recorded."""
    path = log_path(tmp_path, "mac")
    append_op(path, make_add("run", DAY, 45, extra={"distance": 8.2}))
    out = tmp_path / "out.csv"
    sessions, _, _ = replay(read_ops(tmp_path)[0])
    export_csv(sessions.values(), out, metrics=())
    assert "distance" in out.read_text().splitlines()[0]
