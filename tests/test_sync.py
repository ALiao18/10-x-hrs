"""Two-clone integration. The point of all of it: no conflict markers, ever."""

import datetime as dt
import subprocess

import pytest

from tenx import store
from tenx.config import InitError, gitattributes_ok, init, load_config, slugify
from tenx.models import Op, Skill
from tenx.sync import Status, Syncer, git, status_label

DAY = dt.date(2026, 8, 9)
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    return path


def clone(tmp_path, remote, device):
    """A machine: cloned, initialised, and pushed back."""
    root = tmp_path / device
    init(root=root, remote=str(remote), device_id=device)
    syncer = Syncer(root, debounce=0.05)
    syncer.start()
    syncer.flush()
    return root, syncer


def log(root, device, op_id, minutes=60, ts="2026-08-09T10:00:00Z", skill="ml"):
    store.append_op(
        store.log_path(root, device),
        Op(op="add", id=op_id, ts=ts, fields={"skill": skill, "date": DAY, "minutes": minutes}),
    )


def sync(syncer):
    """Pull then push, deterministically: flush's event is only set once the
    worker has drained everything queued ahead of it."""
    syncer.request_pull()
    syncer.flush(timeout=60)


def sessions(root):
    return store.load(root).sessions


def assert_no_conflict_markers(root):
    for path in (root / "sessions").glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        for marker in CONFLICT_MARKERS:
            assert marker not in text, f"{path.name} contains {marker}"


# --- init -------------------------------------------------------------------


def test_init_writes_the_union_merge_rule(tmp_path, remote):
    root, syncer = clone(tmp_path, remote, "mac")
    syncer.stop()
    assert gitattributes_ok(root)
    assert "sessions/*.jsonl merge=union" in (root / ".gitattributes").read_text()
    assert "config.json" in (root / ".gitignore").read_text().splitlines()
    assert store.skills_path(root).exists()
    assert store.log_path(root, "mac").exists()
    assert store.log_path(root, "mac").stat().st_size == 0
    assert load_config(root).device_id == "mac"


def test_init_without_a_remote_still_works(tmp_path):
    report = init(root=tmp_path / "solo", device_id="solo")
    assert gitattributes_ok(report.root)
    syncer = Syncer(report.root)
    assert syncer._compute_status()[0] is Status.LOCAL_ONLY
    assert status_label(Status.LOCAL_ONLY, 0) == "⇅ local only"


def test_init_is_idempotent(tmp_path, remote):
    root = tmp_path / "mac"
    init(root=root, remote=str(remote), device_id="mac")
    init(root=root, remote=str(remote), device_id="mac")
    assert (root / ".gitattributes").read_text().count("merge=union") == 1


def test_init_refuses_a_device_id_another_machine_claimed(tmp_path, remote):
    _, syncer = clone(tmp_path, remote, "mac")
    syncer.stop()
    with pytest.raises(InitError, match="already in the remote"):
        init(root=tmp_path / "impostor", remote=str(remote), device_id="mac")


def test_slugify():
    assert slugify("Mac-Studio.local") == "mac-studio-local"
    assert slugify("  Andrew's Laptop  ") == "andrew-s-laptop"
    assert slugify("!!!") == "device"


# --- the union merge --------------------------------------------------------


def test_two_clones_merge_without_conflicts(tmp_path, remote):
    mac_root, mac = clone(tmp_path, remote, "mac")
    laptop_root, laptop = clone(tmp_path, remote, "laptop")

    log(mac_root, "mac", "MAC1", minutes=120, skill="ml")
    log(laptop_root, "laptop", "LAP1", minutes=30, skill="lc")
    sync(mac)
    sync(laptop)
    sync(mac)

    for root in (mac_root, laptop_root):
        assert_no_conflict_markers(root)
        assert set(sessions(root)) == {"MAC1", "LAP1"}, root.name
    assert sessions(mac_root) == sessions(laptop_root)
    mac.stop()
    laptop.stop()


def test_both_clones_appending_to_the_same_file_union_merges(tmp_path, remote):
    """The case .gitattributes exists for: same second, same log file."""
    mac_root, mac = clone(tmp_path, remote, "mac")
    laptop_root, laptop = clone(tmp_path, remote, "laptop")
    same = "2026-08-09T10:00:00Z"

    # Both machines write into sessions/mac.jsonl - a genuine same-file edit.
    log(mac_root, "mac", "MAC1", ts=same)
    log(laptop_root, "mac", "LAP1", ts=same)
    sync(mac)
    sync(laptop)
    sync(mac)

    for root in (mac_root, laptop_root):
        assert_no_conflict_markers(root)
        assert set(sessions(root)) == {"MAC1", "LAP1"}, root.name
    mac.stop()
    laptop.stop()


def test_offline_appends_land_on_reconnect(tmp_path, remote):
    mac_root, mac = clone(tmp_path, remote, "mac")
    laptop_root, laptop = clone(tmp_path, remote, "laptop")

    # Laptop logs and pushes while the Mac is "offline" (simply not syncing).
    log(laptop_root, "laptop", "LAP1", minutes=30)
    sync(laptop)
    log(mac_root, "mac", "MAC1", minutes=120)
    assert set(sessions(mac_root)) == {"MAC1"}  # local data is usable immediately

    sync(mac)  # reconnect
    assert_no_conflict_markers(mac_root)
    assert set(sessions(mac_root)) == {"MAC1", "LAP1"}
    mac.stop()
    laptop.stop()


def test_a_dirty_working_tree_survives_a_pull(tmp_path, remote):
    mac_root, mac = clone(tmp_path, remote, "mac")
    laptop_root, laptop = clone(tmp_path, remote, "laptop")
    log(mac_root, "mac", "MAC1")
    sync(mac)

    # Uncommitted local change plus an unpulled remote commit.
    store.save_skills(laptop_root, [Skill("ml", "machine learning", "2026-08-09")])
    sync(laptop)

    assert_no_conflict_markers(laptop_root)
    assert [s.id for s in store.load_skills(laptop_root)] == ["ml"], "local edit was lost"
    assert set(sessions(laptop_root)) == {"MAC1"}, "remote commit did not arrive"
    mac.stop()
    laptop.stop()


def test_raw_pull_uses_autostash(tmp_path, remote):
    """--autostash is what recovers a tree the commit step could not stage."""
    mac_root, mac = clone(tmp_path, remote, "mac")
    laptop_root, laptop = clone(tmp_path, remote, "laptop")
    log(mac_root, "mac", "MAC1")
    sync(mac)

    (laptop_root / "skills.json").write_text('{"version": 1, "skills": [], "scratch": true}\n')
    code, _, err = git(laptop_root, "pull", "--rebase", "--autostash", "origin", "main")
    assert code == 0, err
    assert "scratch" in (laptop_root / "skills.json").read_text()
    assert set(sessions(laptop_root)) == {"MAC1"}
    mac.stop()
    laptop.stop()


# --- status -----------------------------------------------------------------


def test_status_reports_local_only_without_a_remote(tmp_path):
    init(root=tmp_path / "solo", device_id="solo")
    syncer = Syncer(tmp_path / "solo", debounce=0.05)
    syncer.start()
    syncer.flush()
    assert syncer.status is Status.LOCAL_ONLY
    syncer.stop()


def test_an_unreachable_remote_never_blocks_entry(tmp_path):
    root = tmp_path / "mac"
    report = init(root=root, remote=str(tmp_path / "nope.git"), device_id="mac")
    assert any("could not clone" in m for m in report.messages)

    log(root, "mac", "MAC1", minutes=45)
    assert set(sessions(root)) == {"MAC1"}, "a bad remote must not stop logging"

    syncer = Syncer(root, debounce=0.05)
    syncer.start()
    sync(syncer)
    assert syncer.status is Status.OFFLINE
    assert status_label(Status.OFFLINE, 0) == "⇅ offline"
    assert (root / "sync.log").exists()
    syncer.stop()

    # The commit is queued locally and lands on the next successful push.
    assert git(root, "log", "--oneline")[1].count("\n") >= 1
    assert set(sessions(root)) == {"MAC1"}


def test_status_counts_commits_ahead(tmp_path, remote):
    root, syncer = clone(tmp_path, remote, "mac")
    syncer.stop()

    log(root, "mac", "MAC1")
    git(root, "add", "-A")
    git(root, "commit", "-m", "log: ml 1h 2026-08-09")
    offline = Syncer(root, debounce=0.05)
    assert offline._compute_status() == (Status.AHEAD, 1)
    assert status_label(Status.AHEAD, 1) == "⇅ 1 ahead"

    offline.start()
    offline.flush()
    assert offline.status is Status.SYNCED
    assert status_label(Status.SYNCED, 0) == "⇅ synced"
    offline.stop()


def test_push_is_debounced_not_immediate(tmp_path, remote):
    root, syncer = clone(tmp_path, remote, "mac")
    syncer.debounce = 30.0
    log(root, "mac", "MAC1")
    syncer.note_write("log: ml 1h 2026-08-09")
    syncer.flush(timeout=60)  # quit forces it through without waiting 30s
    assert syncer._count_ahead() == 0
    syncer.stop()
