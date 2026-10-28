"""Command catalog: spoken phrases → actions, loaded from language TOML files.

Nothing here knows a particular language. A command file provides:

    [keywords]   assistant / dictate / cancel – words that start an utterance
    [numbers]    number words ("drei" = 3) for {n} slots
    filler       words dropped before matching ("bitte", "please")
    [synonyms]   word forms unified before matching ("zum" = "zu", "wechsle" = "wechsel")
    ignore       known Whisper hallucinations on silence
    [messages]   texts shown on screen (OSD, notifications)
    [slots.X]    spoken value → real value, used as {X} in phrases and actions
    [[command]]  id, say = [phrases], do = action(s), help = text for the LLM

Later files override earlier ones: a command with the same id replaces the
old one, slots/keywords/numbers are merged.
"""

from __future__ import annotations

import difflib
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field

SLOT = re.compile(r"\{(\w+)\}")

# User-visible texts; a language file overrides them in [messages].
MESSAGES = {
    "listening_talk": "🎙 Listening …",
    "listening_dictate": "🎙 Dictation …",
    "listening_assistant": "🎙 Assistant …",
    "thinking": "🤖 Thinking …",
    "say_more": "Yes?",
    "cancelled": "Cancelled",
    "unknown": "Unknown command: {text}",
    "failed": "Failed: {what}",
    "stt_error": "Speech recognition not reachable",
    "assistant_error": "Assistant not reachable: {error}",
    "hotkey_talk": "Talk (hold)",
    "hotkey_dictate": "Dictate (hold)",
    "hotkey_assistant": "Ask the assistant (hold)",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\s']", " ", text)        # punctuation, also "E-Mail" → "e mail"
    return " ".join(text.split())


@dataclass
class Command:
    id: str
    say: list[str]
    do: list[dict]
    help: str = ""
    llm: bool = True                 # offered to the assistant as a tool

    @property
    def slots(self) -> list[str]:
        names: list[str] = []
        for phrase in self.say:
            for s in SLOT.findall(phrase):
                if s not in names:
                    names.append(s)
        return names


@dataclass
class Match:
    command: Command
    slots: dict[str, object] = field(default_factory=dict)
    score: float = 1.0


@dataclass
class Catalog:
    commands: dict[str, Command] = field(default_factory=dict)
    slot_values: dict[str, dict[str, str]] = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    numbers: dict[str, int] = field(default_factory=dict)
    filler: list[str] = field(default_factory=list)
    synonyms: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    fuzzy: float = 0.84
    messages: dict[str, str] = field(default_factory=lambda: dict(MESSAGES))

    # --- loading ------------------------------------------------------------

    def add_toml(self, text: str, source: str = "") -> None:
        data = tomllib.loads(text)
        for k, words in data.get("keywords", {}).items():
            self.keywords[k] = [normalize(w) for w in words]
        self.numbers.update({normalize(k): int(v) for k, v in data.get("numbers", {}).items()})
        self.filler += [normalize(w) for w in data.get("filler", [])]
        self.synonyms.update({normalize(k): normalize(v) for k, v in data.get("synonyms", {}).items()})
        self.ignore += [normalize(w) for w in data.get("ignore", [])]
        self.messages.update(data.get("messages", {}))
        for name, values in data.get("slots", {}).items():
            self.slot_values.setdefault(name, {}).update({normalize(k): str(v) for k, v in values.items()})
        for c in data.get("command", []):
            do = c.get("do", [])
            do = [do] if isinstance(do, dict) else list(do)
            if "id" not in c or not c.get("say") or not do:
                raise ValueError(f"{source}: command needs id, say and do: {c}")
            say = [c["say"]] if isinstance(c["say"], str) else list(c["say"])
            self.commands[c["id"]] = Command(id=c["id"], say=say, do=do,
                                             help=c.get("help", ""), llm=c.get("llm", True))

    # --- matching -----------------------------------------------------------

    def clean(self, text: str) -> str:
        """Normalized text without filler words, word forms unified."""
        words = normalize(text).split()
        return " ".join(self.synonyms.get(w, w) for w in words if w not in self.filler)

    def is_noise(self, text: str) -> bool:
        t = normalize(text)
        return not t or t in self.ignore

    def keyword(self, text: str) -> tuple[str, str] | None:
        """("assistant", rest) if the utterance starts with a keyword, longest keyword first."""
        t = normalize(text)
        found = [(len(w), kind, w) for kind, ws in self.keywords.items() for w in ws
                 if t == w or t.startswith(w + " ")]
        if not found:
            return None
        _, kind, w = max(found)
        # Return the rest of the *original* text, so dictation keeps case and punctuation.
        pattern = r"^\W*" + r"[\W_]+".join(re.escape(x) for x in w.split()) + r"\b[\W_]*"
        m = re.match(pattern, unicodedata.normalize("NFKC", text), re.IGNORECASE)
        rest = unicodedata.normalize("NFKC", text)[m.end():] if m else text
        return kind, rest.strip()

    def match(self, text: str) -> Match | None:
        t = self.clean(text)
        if not t:
            return None
        raw = normalize(text)
        best: Match | None = None
        for cmd in self.commands.values():
            for phrase in cmd.say:
                # Free text must not lose filler words or get synonyms replaced.
                m = self._match_phrase(cmd, phrase, raw if "{text}" in phrase else t)
                if m and (best is None or m.score > best.score):
                    best = m
                    if m.score == 1.0:
                        return best
        return best

    def _match_phrase(self, cmd: Command, phrase: str, t: str) -> Match | None:
        if not SLOT.search(phrase):
            p = self.clean(phrase)
            if p == t:
                return Match(cmd)
            score = difflib.SequenceMatcher(None, p, t).ratio()
            return Match(cmd, score=score) if score >= self.fuzzy else None
        # Slot phrase: fixed words must match exactly, slots are resolved (fuzzily) afterwards.
        # A phrase that is nothing but a slot ("{app}") would turn every word that sounds a bit
        # like a slot value into a command, so it needs an exact slot value.
        bare = not SLOT.sub("", phrase).strip()
        cleaner = normalize if "{text}" in phrase else self.clean
        pattern = re.escape(cleaner(SLOT.sub(" uc0slot ", phrase)))
        names = SLOT.findall(phrase)
        for name in names:
            pattern = pattern.replace("uc0slot", f"(?P<{name}>.+?)", 1)
        m = re.fullmatch(pattern, t)
        if not m:
            return None
        slots: dict[str, object] = {}
        score = 0.99
        for name in names:
            value, similarity = self._resolve(name, m.group(name))
            if value is None or (bare and similarity < 1.0):
                return None
            slots[name] = value
            score = min(score, 0.9 + 0.09 * similarity)   # fuzzy slot < exact slot
        return Match(cmd, slots, score=score)

    def _resolve(self, name: str, spoken: str) -> tuple[object | None, float]:
        spoken = normalize(spoken)
        if name == "n":
            if spoken.isdigit():
                return int(spoken), 1.0
            return self.numbers.get(spoken), 1.0
        if name == "text":                      # free text, taken as is
            return spoken, 1.0
        values = self.slot_values.get(name, {})
        if spoken in values:
            return values[spoken], 1.0
        close = difflib.get_close_matches(spoken, values, n=1, cutoff=0.75)
        if not close:
            return None, 0.0
        return values[close[0]], difflib.SequenceMatcher(None, spoken, close[0]).ratio()

    # --- for Whisper and the LLM ---------------------------------------------

    def vocabulary(self, limit: int = 600) -> str:
        """Command phrases as a Whisper prompt (biases recognition, keep it short)."""
        seen, words = set(), []
        for cmd in self.commands.values():
            for phrase in cmd.say:
                if SLOT.search(phrase):
                    continue
                if phrase not in seen:
                    seen.add(phrase)
                    words.append(phrase)
        for values in self.slot_values.values():
            words += [v for v in values if v not in seen]
        return ", ".join(words)[:limit]

    def tools(self) -> list[dict]:
        """Commands as Ollama/OpenAI tool definitions."""
        out = []
        for cmd in self.commands.values():
            if not cmd.llm:
                continue
            props: dict[str, dict] = {}
            for s in cmd.slots:
                if s == "n":
                    props[s] = {"type": "integer", "description": "how many times / number"}
                elif s == "text":
                    props[s] = {"type": "string"}
                else:
                    props[s] = {"type": "string", "enum": sorted(set(self.slot_values.get(s, {}).values()))}
            out.append({"type": "function", "function": {
                "name": tool_name(cmd.id),
                "description": cmd.help or cmd.say[0],
                "parameters": {"type": "object", "properties": props, "required": list(props)},
            }})
        return out

    def by_tool(self, name: str) -> Command | None:
        return next((c for c in self.commands.values() if tool_name(c.id) == name), None)


def tool_name(command_id: str) -> str:
    return re.sub(r"\W", "_", command_id)


