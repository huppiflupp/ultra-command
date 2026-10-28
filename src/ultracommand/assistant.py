"""Local LLM (Ollama) that acts through the same command catalog.

The commands are offered as tools. Every tool call is executed and its result
(or error) goes back to the model, which then decides the next step or gives
a short answer. This loop is what lets the assistant correct itself
("program not found" → try another one) and chain steps.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

from .actions import ActionError, Executor
from .catalog import Catalog
from .desktop import Desktop, DesktopError

SYSTEM = """You are the voice assistant of a Linux KDE Plasma desktop. The user speaks to you; \
their words were transcribed by speech recognition and may contain recognition errors – \
interpret them sensibly.
Act by calling the provided tools. One step at a time is fine: you get each tool's result back \
and can continue, retry differently or stop. Only use tools that exist. Never claim you did \
something you did not do.
When the user wants text written into the current window, call type_text with the finished text \
(not your commentary).
Finish with one short sentence in the user's language: what you did, or the answer to their \
question. It is shown as a small desktop notification, so no markdown and no lists.
Current active window: {window}."""

EXTRA_TOOLS = [
    {"type": "function", "function": {
        "name": "type_text", "description": "Type text into the active window at the cursor",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "get_clipboard", "description": "Read the current clipboard text (e.g. text the user copied)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_active_window", "description": "Application class and title of the active window",
        "parameters": {"type": "object", "properties": {}}}},
]


class Assistant:
    def __init__(self, cfg: dict, catalog: Catalog, executor: Executor, desktop: Desktop,
                 on_step: Callable[[str], None] | None = None):
        self.cfg = cfg["llm"]
        self.catalog = catalog
        self.executor = executor
        self.desktop = desktop
        self.on_step = on_step or (lambda _: None)
        self.history: list[dict] = []
        self.last = 0.0

    def _chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.cfg["model"], "messages": messages, "tools": tools, "stream": False,
                "keep_alive": self.cfg["keep_alive"], "think": False, "options": {"temperature": 0.2}}
        req = urllib.request.Request(self.cfg["url"].rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg["timeout"]) as r:
            return json.loads(r.read())

    def warm_up(self) -> None:
        """Loads the model into VRAM so the first question is fast."""
        body = {"model": self.cfg["model"], "keep_alive": self.cfg["keep_alive"]}
        req = urllib.request.Request(self.cfg["url"].rstrip("/") + "/api/generate",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=self.cfg["timeout"]).read()

    def _tool(self, name: str, args: dict) -> str:
        if name == "type_text":
            self.desktop.type_text(str(args.get("text", "")))
            return "typed"
        if name == "get_clipboard":
            return self.desktop.clipboard()[:4000] or "(clipboard is empty)"
        if name == "get_active_window":
            return f"class={self.desktop.window_class!r} title={self.desktop.window_caption!r}"
        cmd = self.catalog.by_tool(name)
        if cmd is None:
            return f"error: there is no tool {name!r}"
        slots = {}
        for s in cmd.slots:
            value = args.get(s)
            if value is None:
                return f"error: missing argument {s!r}"
            slots[s] = value
        self.on_step(cmd.say[0])
        return self.executor.execute(cmd, slots)

    def ask(self, text: str) -> str:
        now = time.monotonic()
        if now - self.last > self.cfg["memory_seconds"]:
            self.history = []
        window = f"{self.desktop.window_class} – {self.desktop.window_caption}".strip(" –") or "unknown"
        messages = [{"role": "system", "content": SYSTEM.format(window=window)},
                    *self.history, {"role": "user", "content": text}]
        tools = self.catalog.tools() + EXTRA_TOOLS
        answer = ""
        for _ in range(self.cfg["max_steps"]):
            msg = self._chat(messages, tools)["message"]
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                break
            for call in calls:
                fn = call["function"]
                try:
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        args = json.loads(args or "{}")
                    result = self._tool(fn["name"], args)
                except (ActionError, DesktopError, ValueError) as e:
                    result = f"error: {e}"
                messages.append({"role": "tool", "tool_name": fn["name"], "content": result})
        else:
            answer = f"Stopped after {self.cfg['max_steps']} steps."
        # Keep the conversation (without the system prompt) for follow-ups, starting at a
        # user turn – tool results without their tool call would confuse the model.
        keep = messages[1:][-20:]
        while keep and keep[0]["role"] != "user":
            keep.pop(0)
        self.history = keep
        self.last = time.monotonic()
        return answer
