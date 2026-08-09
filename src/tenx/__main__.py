"""`tenx` command line: run the TUI, bootstrap a machine, or log headlessly."""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import store
from .config import InitError, data_dir, init, load_config
from .parse import ParseError, QuickAdd, format_day, format_duration, parse
from .sync import Syncer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tenx", description="log deliberate practice hours")
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser("init", help="set up this machine's data directory")
    setup.add_argument("--remote", help="git remote holding your private log repo")
    setup.add_argument("--device-id", help="override the device id (default: slugified hostname)")

    quick = sub.add_parser("add", help="log a session without opening the TUI")
    quick.add_argument("text", nargs="+", metavar="TEXT", help='e.g. tenx add "ml 1h30 optimizer sweep"')

    args = parser.parse_args(argv)
    if args.command == "init":
        return _init(args.remote, args.device_id)
    if args.command == "add":
        return _add(" ".join(args.text))
    return _run()


def _init(remote: str | None, device_id: str | None) -> int:
    try:
        report = init(remote=remote, device_id=device_id)
    except InitError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1
    for message in report.messages:
        print(f"  {message}")
    print(f"✓ ready - run `tenx` to start logging in {report.root}")
    return 0


def _add(text: str) -> int:
    """The scripting path. Same parser, same log, no TUI."""
    root = data_dir()
    config = load_config(root)
    if config is None:
        print("✗ no data directory yet - run `tenx init` first", file=sys.stderr)
        return 1

    loaded = store.load(root)
    today = dt.date.today()
    intent = parse(text, loaded.skills, today)
    if isinstance(intent, ParseError):
        print(f"✗ {intent.message}", file=sys.stderr)
        return 1
    if not isinstance(intent, QuickAdd):
        print("✗ `tenx add` takes a quick-add line, not a command", file=sys.stderr)
        return 1

    op = store.make_add(intent.skill, intent.date, intent.minutes, intent.note)
    store.append_op(store.log_path(root, config.device_id), op)

    name = next((s.name for s in loaded.skills if s.id == intent.skill), intent.skill)
    total = sum(s.minutes for s in loaded.sessions.values() if s.skill == intent.skill) + intent.minutes
    print(
        f"✓ {name} +{format_duration(intent.minutes)}"
        f" · {format_day(intent.date, today)} · {total / 60:.1f}h total"
    )

    if config.auto_sync:
        syncer = Syncer(root, debounce=0.0)
        syncer.start()
        syncer.note_write(store.commit_message(op))
        syncer.stop()
    return 0


def _run() -> int:
    root = data_dir()
    if load_config(root) is None:
        try:
            report = init(root=root)
        except InitError as error:
            print(f"✗ {error}", file=sys.stderr)
            return 1
        for message in report.messages:
            print(f"  {message}")

    from .app import TenxApp  # imported late so `init`/`add` never need Textual

    TenxApp(root).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
