"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from claude_swap.i18n import localize_bindings, mark


class LocalizedModal(ModalScreen):
    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)


class ConfirmModal(LocalizedModal):
    """Yes/No confirmation. Dismisses with True only on explicit confirm.

    Keyboard-first: ←/→ move between the buttons (Enter presses the focused
    one), y/n answer directly, Esc cancels. Clicking still works.
    """

    BINDINGS = [
        Binding("y", "confirm", mark("Yes"), show=False),
        Binding("n,escape", "cancel", mark("No"), show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, message: str, *, title: str | None = None, yes_label: str | None = None
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        title = self._title if self._title is not None else self.app.gettext("Confirm")
        yes_label = (
            self._yes_label if self._yes_label is not None else self.app.gettext("Yes")
        )
        with Vertical(classes="modal-box"):
            yield Label(title, classes="modal-title")
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button(yes_label, id="yes")
                yield Button(self.app.gettext("Cancel"), id="no")
            yield Static(
                self.app.gettext(
                    "← → · enter  ·  y {answer}  ·  n / esc cancel"
                ).format(answer=yes_label.lower()),
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class TokenForm:
    """What the add-token modal collects."""

    token: str
    email: str | None
    slot: int | None


class AddTokenModal(LocalizedModal):
    """Collects a setup-token/API key, optional email label, optional slot.

    ←/→ only reach the screen when a Button is focused (a focused Input
    consumes them for cursor movement), so they safely double as button
    navigation.
    """

    BINDINGS = [
        Binding("escape", "cancel", mark("Cancel"), show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(
                self.app.gettext("Add account from token"), classes="modal-title"
            )
            yield Static(
                self.app.gettext(
                    "OAuth setup-token (sk-ant-oat…) or Console API key (sk-ant-api…, sk-ant-usr…); the type is auto-detected."
                ),
                classes="modal-body",
            )
            yield Input(
                password=True,
                placeholder=self.app.gettext("token (required)"),
                id="token",
            )
            yield Input(
                placeholder=self.app.gettext("email label (optional)"), id="email"
            )
            yield Input(
                placeholder=self.app.gettext("slot number (optional)"),
                id="slot",
                type="integer",
            )
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button(self.app.gettext("Add"), id="add")
                yield Button(self.app.gettext("Cancel"), id="cancel")
            yield Static(
                self.app.gettext("enter add  ·  tab next field  ·  esc cancel"),
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        email = self.query_one("#email", Input).value.strip() or None
        slot_raw = self.query_one("#slot", Input).value.strip()
        if not token:
            self.query_one("#form-error", Static).update(
                self.app.gettext("Token is required.")
            )
            return
        slot: int | None = None
        if slot_raw:
            try:
                slot = int(slot_raw)
            except ValueError:
                self.query_one("#form-error", Static).update(
                    self.app.gettext("Slot must be a number.")
                )
                return
            if slot < 1:
                self.query_one("#form-error", Static).update(
                    self.app.gettext("Slot must be >= 1.")
                )
                return
        self.dismiss(TokenForm(token=token, email=email, slot=slot))

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(LocalizedModal):
    """Scrollable display of captured (ANSI-colored) action output."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", mark("Close"), show=False)]

    def __init__(self, title: str, output: str) -> None:
        super().__init__()
        self._title = title
        self._output = output

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(
                    Text.from_ansi(
                        self._output.rstrip() or self.app.gettext("(no output)")
                    )
                )
            with Horizontal(classes="modal-buttons"):
                yield Button(self.app.gettext("Close"), id="close")
            yield Static(self.app.gettext("esc close"), classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
