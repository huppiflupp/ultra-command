"""Executes the `do` steps of a command and reports what happened.

Step forms (one key per table, several tables = several steps in order):

    {key = "ctrl+t"}                     key combo, optional repeat = 3 or "{n}",
                                         optional terminal = "ctrl+shift+t" (used in terminals)
    {type = "Hello {text}"}              type text (via clipboard)
    {run = "dolphin"}                    start a program, detached
    {focus = "konsole"}                  switch to its window (next one if active), else start it
    {kwin = "Window Maximize"}           KWin global shortcut by name
    {shortcut = "plasmashell/activate application launcher"}   any global shortcut
    {shell = "playerctl next"}           shell command; its output goes back to the assistant
    {sleep = 0.3}                        wait (seconds)
    {builtin = "repeat"}                 repeat the previous command ("cancel": do nothing)

`{slot}` placeholders are replaced by the values matched in the spoken phrase.
"""

from __future__ import annotations

import time
from typing import Callable

from .catalog import Command
from .desktop import Desktop, DesktopError


MAX_REPEAT = 50                 # "runter 1000" is a recognition error, not a wish


class ActionError(RuntimeError):
    pass


def _fill(value, slots: dict[str, object]):
    if isinstance(value, str):
        for name, v in slots.items():
            value = value.replace("{" + name + "}", str(v))
        return value
    if isinstance(value, list):
        return [_fill(v, slots) for v in value]
    return value


class Executor:
    def __init__(self, desktop: Desktop, builtins: dict[str, Callable[[], str]] | None = None):
        self.desktop = desktop
        self.builtins = builtins or {}

    def execute(self, cmd: Command, slots: dict[str, object] | None = None) -> str:
        """Runs all steps; returns a short report (for the OSD and the assistant)."""
        slots = dict(slots or {})
        if "n" in cmd.slots:
            slots.setdefault("n", 1)            # "runter" = "runter eins"
        report: list[str] = []
        for step in cmd.do:
            step = {k: _fill(v, slots) for k, v in step.items()}
            try:
                out = self._step(step)
            except (DesktopError, ValueError) as e:
                raise ActionError(f"{cmd.id}: {e}") from e
            if out:
                report.append(out)
        return "\n".join(report) or "done"

    def _step(self, step: dict) -> str:
        d = self.desktop
        if "key" in step:
            repeat = int(step.get("repeat", 1))
            if not 1 <= repeat <= MAX_REPEAT:
                raise ValueError(f"repeat {repeat} out of range 1..{MAX_REPEAT}")
            combos = step["terminal"] if "terminal" in step and d.is_terminal() else step["key"]
            for combo in combos if isinstance(combos, list) else [combos]:
                d.key(combo, repeat)
        elif "type" in step:
            d.type_text(str(step["type"]))
        elif "run" in step:
            d.run(step["run"])
        elif "focus" in step:
            return d.focus(step["focus"])
        elif "kwin" in step:
            d.shortcut("kwin", step["kwin"])
        elif "shortcut" in step:
            component, _, name = str(step["shortcut"]).partition("/")
            d.shortcut(component, name)
        elif "shell" in step:
            return d.shell(step["shell"], float(step.get("timeout", 15)))
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "builtin" in step:
            fn = self.builtins.get(step["builtin"])
            if fn is None:
                raise ValueError(f"unknown builtin {step['builtin']!r}")
            return fn()
        else:
            raise ValueError(f"unknown step {step}")
        return ""
