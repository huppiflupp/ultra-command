"""Local LLM (Ollama) that acts through the same command catalog.

The commands are offered as tools. Every tool call is executed and its result
(or error) goes back to the model, which then decides the next step or gives
a short answer. This loop is what lets the assistant correct itself
("program not found" → try another one) and chain steps.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Callable

from .actions import ActionError, Executor
from .catalog import Catalog, Match
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

# For speech that matched no command and had no keyword: it may be misrecognized, background talk
# or noise. The model may only *pick* one command - it does not act freely.
CLASSIFY = (
    "Speech recognition produced a sentence that matched no voice command exactly. Decide whether "
    "the user clearly meant one of these desktop commands:\n{commands}\n\n"
    "Rules: choose a command only if the sentence clearly asks for it. A program counts only if its "
    "name (or an obvious description like 'mail program') is actually in the sentence - a word that "
    "merely sounds similar does not count. Fragments, statements, questions and anything unclear: "
    "command = null."
)



EXTRA_TOOLS = [
    {"type": "function", "function": {
        "name": "type_text", "description": "Type text into the active window at the cursor",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "get_clipboard", "description": "Read the current clipboard text (e.g. text the user copied)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_windows",
        "description": "List the open windows (id, application class, title, which one is active)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "focus_window", "description": "Switch to a window by its id from list_windows",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
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
        self._warming = threading.Lock()

    def _chat(self, messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None,
              timeout: float | None = None) -> dict:
        body = {"model": self.cfg["model"], "messages": messages, "stream": False,
                "keep_alive": self.cfg["keep_alive"], "think": False, "options": {"temperature": 0.2}}
        if tools:
            body["tools"] = tools
        if schema:
            body["format"] = schema
            body["options"]["temperature"] = 0
        req = urllib.request.Request(self.cfg["url"].rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg["timeout"] if timeout is None else timeout) as r:
            return json.loads(r.read())

    def loaded(self) -> bool:
        """Whether the model is in memory right now (Ollama /api/ps)."""
        req = urllib.request.Request(self.cfg["url"].rstrip("/") + "/api/ps")
        with urllib.request.urlopen(req, timeout=3) as r:
            models = json.loads(r.read()).get("models", [])
        want = _with_tag(self.cfg["model"])
        return any(_with_tag(m.get("name", "")) == want or _with_tag(m.get("model", "")) == want for m in models)

    def warm_up_once(self) -> None:
        """Starts loading the model in the background, unless that is already happening."""
        if self._warming.acquire(blocking=False):
            def run():
                try:
                    self.warm_up()
                except OSError:
                    pass
                finally:
                    self._warming.release()
            threading.Thread(target=run, name="warm-up", daemon=True).start()

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
        if name == "list_windows":
            return json.dumps(self.desktop.windows, ensure_ascii=False) if self.desktop.windows \
                else "no window list (KWin script not loaded)"
        if name == "focus_window":
            wid = str(args.get("id", ""))
            if not any(w["id"] == wid for w in self.desktop.windows):
                return f"error: no window with id {wid!r}, call list_windows"
            self.desktop.activate(wid)
            return "activated"
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

    def classify(self, text: str) -> Match | None:
        """Maps unaddressed speech to at most one command - or None. Nothing is executed here.

        Slot values the model picks must really occur in the sentence (as a spoken name or the
        value itself): "Karte" must not become the program "kate".
        """
        lines, ids = [], []
        for c in self.catalog.commands.values():
            # Free text is never guessed, and nothing marked guess = false (closing, sending …).
            if not (c.llm and c.guess) or "text" in c.slots:
                continue
            ids.append(c.id)
            args = self.catalog.slot_schema(c)
            lines.append(f"- {c.id}: {c.help or c.say[0]}" + (f" | args: {json.dumps(args)}" if args else ""))
        messages = [{"role": "system", "content": CLASSIFY.format(commands="\n".join(lines))},
                    {"role": "user", "content": text}]
        # The grammar only allows real command ids (or null).
        schema = {"type": "object", "required": ["command", "args"],
                  "properties": {"command": {"enum": [*ids, None]}, "args": {"type": "object"}}}
        out = json.loads(self._chat(messages, schema=schema, timeout=self.cfg["classify_timeout"])
                         ["message"]["content"])
        cmd = self.catalog.commands.get(out.get("command") or "")
        if cmd is None or cmd.id not in ids:
            return None
        said = self.catalog.clean(text).split()
        slots: dict[str, object] = {}
        for s in cmd.slots:
            value = (out.get("args") or {}).get(s)
            if s == "n":
                # The number must have been said – the model must not invent "50 times" or
                # "desktop 1". Several numbers: unclear which one is meant. A missing count
                # means once; a missing target (desktop number) means no command.
                spoken_n = [n for n in (self.catalog.number(w) for w in said) if n is not None]
                if len(spoken_n) == 1:
                    slots[s] = spoken_n[0]
                elif _is_count(cmd):
                    slots[s] = 1
                else:
                    return None
                continue
            allowed = self.catalog.slot_values.get(s, {})
            if value not in allowed.values():
                return None                         # only values from the slot list, never "reboot"
            names = [k for k, v in allowed.items() if v == value] + [str(value)]
            if not any(_contains(said, self.catalog.clean(n).split()) for n in names):
                return None                         # the name must really have been said
            slots[s] = value
        return Match(cmd, slots, score=0.5)

    def ask(self, text: str) -> str:
        """Lets the model act on `text` through the tools, with results fed back."""
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


def _contains(words: list[str], part: list[str]) -> bool:
    """True if `part` occurs as consecutive words in `words`."""
    n = len(part)
    return n > 0 and any(words[i:i + n] == part for i in range(len(words) - n + 1))


def _with_tag(name: str) -> str:
    """Ollama names carry a tag: "qwen3.5" is "qwen3.5:latest"."""
    return name if ":" in name else f"{name}:latest"


def _is_count(cmd) -> bool:
    """{n} is a count when it only feeds `repeat` – otherwise it names something (desktop 2)."""
    return all("{n}" not in str(v) or k == "repeat" for step in cmd.do for k, v in step.items())
