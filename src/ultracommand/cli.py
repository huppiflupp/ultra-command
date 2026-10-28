"""Command line: run the daemon, control it, test phrases without side effects."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

from . import config


def _proxy():
    import dbus
    from .daemon import BUS_NAME, OBJ_PATH
    try:
        return dbus.Interface(dbus.SessionBus().get_object(BUS_NAME, OBJ_PATH), BUS_NAME)
    except dbus.DBusException:
        sys.exit("ultra-command is not running (systemctl --user start ultra-command)")


def cmd_match(cfg: dict, args) -> None:
    """Shows what an utterance would do – nothing is executed."""
    from .daemon import load_catalog
    cat = load_catalog(cfg)
    text = " ".join(args.text)
    if cat.is_noise(text):
        print("noise / ignored")
        return
    kw = cat.keyword(text)
    if kw:
        print(f"keyword → {kw[0]}: {kw[1]!r}")
        return
    m = cat.match(text)
    if m:
        print(f"command {m.command.id} {m.slots or ''} score {m.score:.2f}")
        for step in m.command.do:
            print(f"  {step}")
    else:
        print(f"no command → fallback: {cfg['route']['fallback']}")


def cmd_commands(cfg: dict, args) -> None:
    from .daemon import load_catalog
    cat = load_catalog(cfg)
    for c in cat.commands.values():
        print(f"{c.id:28} {' | '.join(c.say)}")
    for kind, words in cat.keywords.items():
        print(f"keyword {kind:20} {' | '.join(words)}")


def cmd_transcribe(cfg: dict, args) -> None:
    from . import stt
    from .daemon import load_catalog
    prompt = config.whisper_prompt(cfg, load_catalog(cfg).prompt) if args.prompt else ""
    print(stt.transcribe(Path(args.wav).read_bytes(), cfg["whisper"]["url"], cfg["language"], prompt))


def cmd_check(cfg: dict, args) -> None:
    ok = True

    def report(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✓' if good else '✗'} {name}{': ' + detail if detail else ''}")

    for tool in ("pw-record", "ydotool", "systemd-run"):
        report(tool, bool(shutil.which(tool)))
    from .desktop import Desktop
    sock = Desktop(cfg).env["YDOTOOL_SOCKET"]
    report("ydotoold socket", os.path.exists(sock), sock)
    try:
        urllib.request.urlopen(cfg["whisper"]["url"].rsplit("/", 1)[0] + "/", timeout=3).read()
        report("whisper server", True, cfg["whisper"]["url"])
    except OSError as e:
        report("whisper server", False, f"{cfg['whisper']['url']}: {e}")
    try:
        tags = json.loads(urllib.request.urlopen(cfg["llm"]["url"] + "/api/tags", timeout=3).read())
        names = {m["name"] for m in tags.get("models", [])}
        report("ollama model", cfg["llm"]["model"] in names, cfg["llm"]["model"])
    except OSError as e:
        report("ollama", False, str(e))
    try:
        import dbus
        bus = dbus.SessionBus()
        for svc in ("org.kde.kglobalaccel", "org.kde.klipper", "org.kde.KWin", "org.kde.plasmashell"):
            report(svc, bool(bus.name_has_owner(svc)))
    except Exception as e:
        report("D-Bus", False, str(e))
    from .daemon import load_catalog
    try:
        report("commands", True, f"{len(load_catalog(cfg).commands)} loaded")
    except Exception as e:
        report("commands", False, str(e))
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="ultra-command", description="Voice control for the Linux desktop")
    ap.add_argument("--config", type=Path, help=f"config file (default {config.CONFIG_FILE})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daemon")
    for name in ("toggle", "start"):
        p = sub.add_parser(name, help=f"{name} recording in the running daemon")
        p.add_argument("mode", nargs="?", default="talk", choices=["talk", "dictate", "assistant"])
    sub.add_parser("stop", help="stop recording")
    p = sub.add_parser("say", help="let the running daemon handle a text as if spoken")
    p.add_argument("text", nargs="+")
    p.add_argument("--mode", default="talk", choices=["talk", "dictate", "assistant"])
    p = sub.add_parser("match", help="show what a phrase would do (dry run)")
    p.add_argument("text", nargs="+")
    sub.add_parser("commands", help="list commands and keywords")
    sub.add_parser("windows", help="windows as the running daemon sees them")
    p = sub.add_parser("transcribe", help="transcribe a 16 kHz WAV file")
    p.add_argument("wav")
    p.add_argument("--prompt", action="store_true", help="use the context sentence, as the daemon does")
    sub.add_parser("check", help="check that everything ultra-command needs is there")
    args = ap.parse_args()
    cfg = config.load(args.config)

    if args.cmd == "run":
        from .daemon import main as run
        run(cfg)
    elif args.cmd == "toggle":
        _proxy().Toggle(args.mode)
    elif args.cmd == "start":
        _proxy().Start(args.mode)
    elif args.cmd == "stop":
        _proxy().Stop()
    elif args.cmd == "say":
        _proxy().Say(" ".join(args.text), args.mode)
    elif args.cmd == "windows":
        for w in json.loads(str(_proxy().ListWindows())):
            print(f"{'*' if w['active'] else ' '} {w['cls']:28} {w['caption'][:70]}")
    else:
        {"match": cmd_match, "commands": cmd_commands, "transcribe": cmd_transcribe,
         "check": cmd_check}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
