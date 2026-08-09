"""Per-machine config, data-dir location, and `tenx init`."""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from . import store
from .sync import CLONE_TIMEOUT, git, has_remote

GITATTRIBUTES_LINE = "sessions/*.jsonl merge=union"
# sync.log and the skills.json temp file live in the data dir, so they need
# ignoring too or `git add -A` would commit them.
GITIGNORE_LINES = ("config.json", "sync.log", "*.tmp")
DEVICE_RE = re.compile(r"^[a-z0-9-]+$")


class InitError(Exception):
    """Setup cannot continue without a human decision."""


@dataclass
class Config:
    device_id: str
    auto_sync: bool = True
    push_debounce_seconds: int = 30


@dataclass
class InitReport:
    root: Path
    device_id: str
    cloned: bool
    messages: list[str]


def data_dir() -> Path:
    """`$TENX_HOME` or `~/.tenx`. The override is what makes tests hermetic."""
    override = os.environ.get("TENX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".tenx"


def config_path(root: Path) -> Path:
    return root / "config.json"


def load_config(root: Path) -> Config | None:
    path = config_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    device_id = str(data.get("device_id") or "")
    if not DEVICE_RE.match(device_id):
        return None
    return Config(
        device_id=device_id,
        auto_sync=bool(data.get("auto_sync", True)),
        push_debounce_seconds=int(data.get("push_debounce_seconds", 30)),
    )


def save_config(root: Path, config: Config) -> None:
    root.mkdir(parents=True, exist_ok=True)
    config_path(root).write_text(
        json.dumps(
            {
                "device_id": config.device_id,
                "auto_sync": config.auto_sync,
                "push_debounce_seconds": config.push_debounce_seconds,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", text.strip().lower()).strip("-")
    return slug or "device"


def default_device_id() -> str:
    return slugify(socket.gethostname().split(".")[0] or "device")


def gitattributes_ok(root: Path) -> bool:
    path = root / ".gitattributes"
    return path.exists() and GITATTRIBUTES_LINE in path.read_text(encoding="utf-8").splitlines()


def _ensure_line(path: Path, line: str) -> bool:
    """Append `line` unless already present. Returns True when it wrote."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if line in text.splitlines():
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line + "\n", encoding="utf-8")
    return True


def _ensure_identity(root: Path) -> None:
    """A box with no global git identity cannot commit at all."""
    code, out, _ = git(root, "config", "user.email")
    if code != 0 or not out:
        git(root, "config", "user.email", "tenx@localhost")
        git(root, "config", "user.name", "tenx")


def _nonempty_dir(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def init(root: Path | None = None, remote: str | None = None, device_id: str | None = None) -> InitReport:
    root = Path(root) if root is not None else data_dir()
    messages: list[str] = []
    cloned = False

    if not (root / ".git").is_dir():
        if remote and not _nonempty_dir(root):
            root.parent.mkdir(parents=True, exist_ok=True)
            code, _, err = git(root.parent, "clone", remote, str(root), timeout=CLONE_TIMEOUT)
            if code == 0:
                cloned = True
                messages.append(f"cloned {remote}")
            else:
                messages.append(f"could not clone {remote} - starting a fresh repo ({err.splitlines()[-1] if err else 'unknown error'})")
        if not (root / ".git").is_dir():
            root.mkdir(parents=True, exist_ok=True)
            code, _, err = git(root, "init", "-b", "main")
            if code != 0:
                raise InitError(f"git init failed in {root}: {err}")
            messages.append(f"created {root}")

    if remote and not has_remote(root):
        git(root, "remote", "add", "origin", remote)
    _ensure_identity(root)

    if _ensure_line(root / ".gitattributes", GITATTRIBUTES_LINE):
        messages.append("wrote .gitattributes (union merge)")
    for line in GITIGNORE_LINES:
        _ensure_line(root / ".gitignore", line)
    if not store.skills_path(root).exists():
        store.save_skills(root, [])
        messages.append("created skills.json")

    existing = load_config(root)
    resolved = device_id or (existing.device_id if existing else None) or default_device_id()
    if not DEVICE_RE.match(resolved):
        raise InitError(f'device id "{resolved}" must match [a-z0-9-]+')

    # A log that is *tracked* came from the remote, so another machine claimed
    # this id. Checking the file size instead would miss it: a freshly
    # initialised log is zero bytes until that machine logs its first session.
    claimed = git(root, "ls-files", "--error-unmatch", f"sessions/{resolved}.jsonl")[0] == 0
    if existing is None and claimed:
        raise InitError(
            f'sessions/{resolved}.jsonl is already in the remote, so another machine has claimed "{resolved}".\n'
            f"Two machines sharing a device id write to one file and will conflict. Re-run with:\n"
            f"    tenx init --device-id <another-name>\n"
            f"(If this *is* that machine and you only lost config.json, re-run with --device-id {resolved}.)"
        )

    store.ensure_log(store.log_path(root, resolved))
    save_config(root, existing or Config(device_id=resolved))

    if not gitattributes_ok(root):
        raise InitError(f"{root}/.gitattributes is missing the union-merge rule; concurrent logs would conflict")

    git(root, "add", "-A")
    git(root, "commit", "-m", f"tenx: init {resolved}")
    messages.append(f"device id: {resolved}")
    return InitReport(root=root, device_id=resolved, cloned=cloned, messages=messages)
