"""The always-focused input, plus the echo/error line under it."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

ECHO_SECONDS = 2.0


class CommandBar(Vertical):
    """Entry speed is the product, so this widget never loses focus and never
    clears the input on a failure - a typo is fixed in place."""

    last_message = ""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="ml 1h30 attention ablation      (: for commands)", id="entry")
        yield Static("", id="echo")

    @property
    def entry(self) -> Input:
        return self.query_one("#entry", Input)

    @property
    def echo(self) -> Static:
        return self.query_one("#echo", Static)

    def ok(self, message: str) -> None:
        self.last_message = f"✓ {message}"
        self.entry.value = ""
        self.echo.remove_class("error")
        self.echo.update(f"✓ {message}")
        self.set_timer(ECHO_SECONDS, self._clear_echo)

    def fail(self, message: str) -> None:
        self.last_message = f"✗ {message}"
        self.echo.add_class("error")
        self.echo.update(f"✗ {message}")

    def note(self, message: str) -> None:
        """Append a warning to whatever the echo already says, without
        disturbing the entry - used to raise conflicts on the log itself."""
        self.last_message = f"{self.last_message}  ·  {message}" if self.last_message else message
        self.echo.update(self.last_message)

    def _clear_echo(self) -> None:
        self.echo.update("")
