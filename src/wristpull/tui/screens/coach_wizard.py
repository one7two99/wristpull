"""Modal with the goal-wizard questions; returns the answers dict (or None when cancelled)."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet

from ...coach.profile import load_answers
from ...coach.wizard import QUESTIONS, Question
from ..i18n import _


def _wid(question: Question, value: str = "") -> str:
    base = f"q-{question.key}"
    return f"{base}-{value}" if value else base


class WizardScreen(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+s", "save", "Save", show=False)]
    DEFAULT_CSS = """
    WizardScreen { align: center middle; }
    WizardScreen > VerticalScroll { width: 100; max-width: 100%; height: 90%; border: round $primary; padding: 1 2; background: $surface; }
    WizardScreen .q-title { text-style: bold; margin-top: 1; }
    WizardScreen RadioSet, WizardScreen Checkbox { width: 100%; }
    """

    def __init__(self, config, lang: str) -> None:
        super().__init__()
        self.config = config
        self.lang = lang if lang in ("en", "de") else "en"
        self.previous = load_answers(config)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="wizard"):
            yield Label(_("Goal wizard: your choices become the first part of the goal profile; your own notes stay untouched."), classes="q-title")
            for q in QUESTIONS:
                prev = self.previous.get(q.key)
                yield Label(q.text[self.lang], classes="q-title")
                if not q.options:
                    yield Input(value=str(prev) if prev else "", placeholder=q.hint.get(self.lang, ""), id=_wid(q))
                elif q.multi:
                    chosen = set(prev) if isinstance(prev, list) else set()
                    for value, label in q.options:
                        yield Checkbox(label[self.lang], value=value in chosen, id=_wid(q, value))
                else:
                    with RadioSet(id=_wid(q)):
                        for value, label in q.options:
                            yield RadioButton(label[self.lang], value=(prev == value), id=_wid(q, value))
            with Horizontal(classes="row"):
                yield Button(_("Save profile"), id="wizard-save", variant="primary")
                yield Button(_("Cancel"), id="wizard-cancel")

    def collect(self) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for q in QUESTIONS:
            if not q.options:
                text = self.query_one(f"#{_wid(q)}", Input).value.strip()
                if text:
                    answers[q.key] = text
            elif q.multi:
                chosen = [value for value, _label in q.options if self.query_one(f"#{_wid(q, value)}", Checkbox).value]
                if chosen:
                    answers[q.key] = chosen
            else:
                pressed = self.query_one(f"#{_wid(q)}", RadioSet).pressed_button
                if pressed is not None and pressed.id:
                    answers[q.key] = pressed.id.removeprefix(_wid(q) + "-")
        return answers

    def action_save(self) -> None:
        self.dismiss(self.collect())

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#wizard-save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#wizard-cancel")
    def _cancel(self) -> None:
        self.action_cancel()
