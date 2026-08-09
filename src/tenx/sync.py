"""Git-backed sync. No Textual imports.

Every git invocation after `init` happens on one worker thread, which is what
keeps `index.lock` uncontended and keeps entry off the network path. The UI
thread appends to the log itself and returns immediately; committing and
pushing are queued behind it.
"""

from __future__ import annotations

import datetime as dt
import os
import queue
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable

GIT_TIMEOUT = 20
CLONE_TIMEOUT = 120
NO_REMOTE_BRANCH = "couldn't find remote ref"


class Status(Enum):
    LOCAL_ONLY = "local only"
    SYNCING = "syncing"
    SYNCED = "synced"
    AHEAD = "ahead"
    OFFLINE = "offline"


def _env() -> dict[str, str]:
    """Never let git block on a credential or host-key prompt: a hung prompt
    would look like a hang instead of `⇅ offline`."""
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
    }


def git(root: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run git in `root`. Returns (returncode, stdout, stderr), never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0] if args else ''} timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "git is not installed"


def has_remote(root: Path) -> bool:
    code, out, _ = git(root, "remote")
    return code == 0 and bool(out)


def current_branch(root: Path) -> str:
    code, out, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 and out and out != "HEAD" else "main"


def log_failure(root: Path, message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(root / "sync.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


class Syncer:
    """Queued pull / commit / debounced push.

    `note_write` and `request_pull` return immediately. `flush` blocks only on
    quit, which is the one place the spec allows waiting.
    """

    def __init__(
        self,
        root: Path,
        debounce: float = 30.0,
        on_status: Callable[[Status, int], None] | None = None,
    ) -> None:
        self.root = root
        self.debounce = debounce
        self.on_status = on_status
        self.status = Status.LOCAL_ONLY
        self.ahead = 0
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._deadline: float | None = None
        self._offline = False

    # -- public, called from the UI thread -----------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tenx-sync", daemon=True)
        self._thread.start()

    def request_pull(self) -> None:
        self._queue.put(("pull", None))

    def note_write(self, message: str) -> None:
        self._queue.put(("commit", message))

    def flush(self, timeout: float = 10.0) -> None:
        done = threading.Event()
        self._queue.put(("flush", done))
        done.wait(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        done = threading.Event()
        self._queue.put(("stop", done))
        done.wait(timeout)
        self._thread.join(timeout=1.0)
        self._thread = None

    # -- worker --------------------------------------------------------------

    def _run(self) -> None:
        self._emit()
        while True:
            wait = None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
            try:
                kind, payload = self._queue.get(timeout=wait)
            except queue.Empty:
                self._push()
                self._deadline = None
                self._emit()
                continue
            if kind == "pull":
                self._pull()
            elif kind == "commit":
                self._commit(str(payload))
                self._deadline = time.monotonic() + self.debounce
            elif kind in ("flush", "stop"):
                self._push()
                self._deadline = None
                self._emit()
                if isinstance(payload, threading.Event):
                    payload.set()
                if kind == "stop":
                    return
                continue
            self._emit()

    def _commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        code, _, err = git(self.root, "commit", "-m", message)
        if code != 0 and "nothing to commit" not in err.lower():
            log_failure(self.root, f"commit failed: {err}")

    def _pull(self) -> None:
        if not has_remote(self.root):
            return
        self.status = Status.SYNCING
        self._emit()
        # Commit first so a rebase never has to stash a freshly logged line.
        self._commit("log: sync")
        branch = current_branch(self.root)
        code, _, err = git(
            self.root, "pull", "--rebase", "--autostash", "origin", branch, timeout=GIT_TIMEOUT
        )
        if code == 0 or NO_REMOTE_BRANCH in err.lower():
            self._offline = False
        else:
            self._offline = True
            log_failure(self.root, f"pull failed: {err}")

    def _push(self) -> None:
        if not has_remote(self.root):
            return
        self._commit("log: sync")
        code, _, err = git(self.root, "push", "-u", "origin", current_branch(self.root))
        if code == 0:
            self._offline = False
        else:
            self._offline = True
            log_failure(self.root, f"push failed: {err}")

    def _emit(self) -> None:
        self.status, self.ahead = self._compute_status()
        if self.on_status is not None:
            self.on_status(self.status, self.ahead)

    def _compute_status(self) -> tuple[Status, int]:
        if not has_remote(self.root):
            return Status.LOCAL_ONLY, 0
        if self._offline:
            return Status.OFFLINE, self._count_ahead()
        ahead = self._count_ahead()
        return (Status.AHEAD, ahead) if ahead else (Status.SYNCED, 0)

    def _count_ahead(self) -> int:
        code, out, _ = git(self.root, "rev-list", "--count", "@{u}..HEAD")
        return int(out) if code == 0 and out.isdigit() else 0


def status_label(status: Status, ahead: int) -> str:
    if status is Status.AHEAD and ahead:
        return f"⇅ {ahead} ahead"
    return f"⇅ {status.value}"
