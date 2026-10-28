"""Configuration: built-in defaults, overridden by ~/.config/ultra-command/config.toml."""

from __future__ import annotations

import copy
import os
import sys
import tomllib
from importlib import resources
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ultra-command"
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULTS: dict = {
    "language": "de",
    "hotkeys": {
        # Hold to talk. "talk" routes automatically (command → assistant keyword → fallback),
        # "dictate" always types, "assistant" always asks the LLM. Empty = not registered.
        "talk": "ScrollLock",
        "dictate": "",
        "assistant": "",
    },
    "whisper": {
        "url": "http://127.0.0.1:8097/inference",
        "timeout": 30,
        # Context for Whisper: "" = the `prompt` sentence of the language file, "off" = none,
        # anything else is used as is. A describing sentence, never a comma list (Whisper copies
        # the style) and never a command (Whisper may echo it on silence).
        "prompt": "",
    },
    "audio": {
        "max_seconds": 60,
        # Toggle mode (`ultra-command toggle`) only: stop after this much silence once speech began.
        "silence_seconds": 1.2,
        "silence_level": 500,          # RMS of 16-bit samples
        # Recordings whose loudest 100 ms stay below this are room noise and not sent to
        # Whisper (it would invent text). 0 = off. Check `journalctl` for "too quiet".
        "speech_level": 400,
        # Keep the last N recordings with their transcript for tuning (0 = keep none).
        # Never for the dictate mode/keyword – but "talk" utterances can be private too.
        "keep_recordings": 0,
    },
    "route": {
        # What to do with speech that is neither a command nor starts with a keyword:
        # "guess" (the LLM may pick one command, nothing else), "assistant" (the LLM acts
        # freely, like after "Computer"), "dictate" (type it) or "ignore".
        "fallback": "guess",
        "fuzzy": 0.84,                 # minimum similarity for a command match (0..1)
    },
    "input": {
        "layout": "",                  # keyboard layout for ydotool; "" = take from language
        "paste_key": "ctrl+v",
        "terminal_paste_key": "ctrl+shift+v",
        "terminals": ["konsole", "org.kde.konsole", "yakuake", "kitty", "alacritty", "foot",
                      "wezterm", "org.wezfurlong.wezterm", "gnome-terminal-server", "xterm",
                      "com.mitchellh.ghostty", "ghostty"],
        "restore_clipboard": True,
        "ydotool_socket": "",          # "" = $YDOTOOL_SOCKET or /run/user/<uid>/.ydotool_socket
    },
    "llm": {
        "url": "http://127.0.0.1:11434",
        "model": "gemma4:12b-it-qat",
        "keep_alive": "30m",
        "max_steps": 6,                # tool calls per request before giving up
        "memory_seconds": 120,         # follow-up questions within this time keep context
        "timeout": 120,
        "classify_timeout": 15,        # "guess" fallback (only runs while the model is loaded)
    },
    "feedback": {
        "osd": True,                   # Plasma OSD for short confirmations
        "notify_seconds": 8,           # LLM answers as notification
    },
    # Extra command files, merged after the built-in file for `language`.
    "commands": [str(CONFIG_DIR / "commands.toml")],
}


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = path or CONFIG_FILE
    if path.exists():
        with open(path, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    if "prompt_commands" in cfg["whisper"]:
        if cfg["whisper"].pop("prompt_commands") is False and not cfg["whisper"]["prompt"]:
            cfg["whisper"]["prompt"] = "off"          # keep the user's "no prompt" choice
        print("config: whisper.prompt_commands is replaced by whisper.prompt (\"off\" to disable)",
              file=sys.stderr, flush=True)
    if not cfg["input"]["layout"]:
        cfg["input"]["layout"] = cfg["language"]
    return cfg


def builtin_commands(language: str) -> str | None:
    """Text of the shipped command file for a language, if there is one."""
    f = resources.files("ultracommand") / "data" / f"commands-{language}.toml"
    return f.read_text(encoding="utf-8") if f.is_file() else None


def whisper_prompt(cfg: dict, language_prompt: str) -> str:
    """The context sentence for Whisper: config value, "off", or the language file's sentence."""
    p = cfg["whisper"]["prompt"]
    return "" if p == "off" else (p or language_prompt)
