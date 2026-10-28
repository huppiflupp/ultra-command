"""The ultra-command daemon: hotkeys → recording → Whisper → command / dictation / assistant.

Runs a GLib main loop for D-Bus (KDE hotkeys, the KWin script, the control
interface). Recognition and actions run on a worker thread, one utterance
after the other.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import threading
import time
import traceback
from importlib import resources
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from . import config, keys, stt
from .actions import ActionError, Executor, _fill
from .assistant import Assistant
from .audio import Recorder
from .catalog import Catalog, Match
from .desktop import Desktop, DesktopError, load_kwin_script, unload_kwin_script

BUS_NAME = "io.github.huppiflupp.UltraCommand"
OBJ_PATH = "/io/github/huppiflupp/UltraCommand"
KWIN_SCRIPT = "ultra-command-active-window"
MODES = ("talk", "dictate", "assistant")
RECORDINGS = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ultra-command" / "recordings"


def load_catalog(cfg: dict) -> Catalog:
    cat = Catalog(fuzzy=cfg["route"]["fuzzy"])
    text = config.builtin_commands(cfg["language"])
    if text:
        cat.add_toml(text, f"commands-{cfg['language']}.toml")
    for p in cfg["commands"]:
        p = Path(p).expanduser()
        if p.exists():
            cat.add_toml(p.read_text(encoding="utf-8"), str(p))
    return cat


class Brain:
    """Everything that turns text into actions – usable without audio (`ultra-command say`)."""

    def __init__(self, cfg: dict, desktop: Desktop, catalog: Catalog, log=print):
        self.cfg = cfg
        self.desktop = desktop
        self.catalog = catalog
        self.log = log
        self.last: Match | None = None
        self.prompt = config.whisper_prompt(cfg, catalog.prompt)     # what Whisper gets in talk mode
        self.executor = Executor(desktop, {"repeat": self._repeat, "cancel": lambda: ""})
        self.assistant = Assistant(cfg, catalog, self.executor, desktop,
                                   on_step=lambda s: desktop.osd(f"🤖 {s}"))

    def msg(self, key: str, **kw) -> str:
        return self.catalog.messages.get(key, key).format(**kw)

    def _repeat(self) -> str:
        if not self.last:
            raise ValueError("nothing to repeat")
        return self.executor.execute(self.last.command, self.last.slots)

    def handle(self, text: str, mode: str = "talk") -> None:
        if self.catalog.is_noise(text) or (mode != "dictate" and self.catalog.is_prompt_echo(text, self.prompt)):
            self.log(f"(noise) {text!r}")
            return
        self.log(f"[{mode}] {text!r}")
        if mode == "dictate":
            return self.dictate(text)
        if mode == "assistant":
            return self.ask(text)
        kw = self.catalog.keyword(text)
        if kw:
            kind, rest = kw
            if kind == "assistant":
                return self.ask(rest) if rest else self.desktop.osd(self.msg("say_more"))
            if kind == "dictate":
                return self.dictate(rest)
            if kind == "cancel":
                return self.desktop.osd(self.msg("cancelled"))
        m = self.catalog.match(text)
        if m:
            return self.run(m)
        fallback = self.cfg["route"]["fallback"]
        self.log(f"  → no command, fallback {fallback}")
        if fallback == "dictate":
            self.dictate(text)
        elif fallback == "guess":
            self.guess(text)
        elif fallback == "assistant":
            self.ask(text)
        else:
            self.desktop.osd(self.msg("unknown", text=text))

    def run(self, m: Match) -> None:
        self.log(f"  → {m.command.id} {m.slots or ''} ({m.score:.2f})")
        what = _fill(m.command.say[0], m.slots)
        try:
            self.executor.execute(m.command, m.slots)
        except ActionError as e:
            self.log(f"  ! {e}")
            self.desktop.osd(self.msg("failed", what=what), "dialog-error")
            return
        if m.command.do[0].get("builtin") != "repeat":
            self.last = m
        self.desktop.osd(f"✓ {what}")

    def dictate(self, text: str) -> None:
        if not text:
            return
        if text[-1] in ".!?:;,…":
            text += " "
        try:
            self.desktop.type_text(text)
            self.log(f"  ⌨ typed into {self.desktop.window_class or 'unknown window'}")
        except DesktopError as e:
            self.log(f"  ! {e}")
            self.desktop.osd(self.msg("failed", what="type"), "dialog-error")

    def _llm_failed(self, what: str, e: Exception) -> None:
        self.log(f"  ! {what}: {e}")
        self.desktop.notify("Ultra-Command", self.msg("assistant_error", error=e), "dialog-error")

    def guess(self, text: str) -> None:
        """Unaddressed speech that matched no command: let the model pick one command, or nothing.

        Loading the model takes long and would hold up the next real command, so while it is
        not in memory nothing is guessed – it gets loaded in the background instead.
        """
        try:
            if not self.assistant.loaded():
                self.log("  🤖 model not loaded – loading it, no guess this time")
                self.assistant.warm_up_once()
                self.desktop.osd(self.msg("unknown", text=text), "dialog-warning")
                return
            self.desktop.osd(self.msg("thinking"), "dialog-question")
            m = self.assistant.classify(text)
        except TimeoutError:                        # model was pushed out meanwhile – just a miss
            self.log("  🤖 guess timed out")
            m = None
        except Exception as e:                      # network, model missing, bad JSON ...
            return self._llm_failed("classify", e)
        if m:
            self.log("  🤖 guessed:")
            self.run(m)
        else:
            self.log("  🤖 no command")
            self.desktop.osd(self.msg("unknown", text=text), "dialog-warning")

    def ask(self, text: str) -> None:
        self.desktop.osd(self.msg("thinking"), "dialog-question")
        try:
            answer = self.assistant.ask(text)
        except Exception as e:                      # network, model missing, bad JSON …
            return self._llm_failed("assistant", e)
        self.log(f"  🤖 {answer}")
        if answer:
            self.desktop.notify(text, answer, "dialog-information")


class Service(dbus.service.Object):
    def __init__(self, daemon: "Daemon", bus: dbus.Bus):
        self.daemon = daemon
        dbus.service.Object.__init__(self, dbus.service.BusName(BUS_NAME, bus, do_not_queue=True), OBJ_PATH)

    @dbus.service.method(BUS_NAME, in_signature="s")
    def Start(self, mode):
        self.daemon.start(str(mode) or "talk")

    @dbus.service.method(BUS_NAME)
    def Stop(self):
        self.daemon.stop()

    @dbus.service.method(BUS_NAME, in_signature="s")
    def Toggle(self, mode):
        self.daemon.toggle(str(mode) or "talk")

    @dbus.service.method(BUS_NAME, in_signature="ss")
    def Say(self, text, mode):
        """Process text as if it had been spoken (testing, scripting)."""
        self.daemon.jobs.put(("text", str(text), str(mode) or "talk"))

    @dbus.service.method(BUS_NAME, out_signature="s")
    def ListWindows(self):
        return json.dumps(self.daemon.desktop.windows, ensure_ascii=False)

    @dbus.service.method(BUS_NAME, in_signature="s")
    def Windows(self, data):
        try:
            self.daemon.desktop.set_windows(str(data))
        except ValueError:
            pass


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.desktop = Desktop(cfg)
        self.catalog = load_catalog(cfg)
        self.brain = Brain(cfg, self.desktop, self.catalog, log=self.log)
        a = cfg["audio"]
        self.recorder = Recorder(a["max_seconds"], a["silence_seconds"], a["silence_level"], a["speech_level"])
        self.mode = "talk"
        self.jobs: queue.Queue = queue.Queue()


    @staticmethod
    def log(text: str) -> None:
        print(text, flush=True)

    # --- recording ---------------------------------------------------------------

    def start(self, mode: str, auto_stop: bool = False) -> None:
        if mode not in MODES or self.recorder.active:
            return
        self.mode = mode
        on_silence = (lambda: GLib.idle_add(self.stop)) if auto_stop else None
        self.recorder.start(on_silence)
        self.desktop.osd(self.brain.msg(f"listening_{mode}"))

    def stop(self) -> bool:
        wav = self.recorder.stop()
        if wav:
            self.jobs.put(("audio", wav, self.mode))
        elif self.recorder.loudest:
            self.log(f"too quiet (loudest {self.recorder.loudest:.0f} < speech_level "
                     f"{self.cfg['audio']['speech_level']}) – not sent to Whisper")
            self.desktop.osd(self.brain.msg("too_quiet"), "dialog-warning")
        return False                                  # for GLib.idle_add: run once

    def toggle(self, mode: str) -> None:
        if self.recorder.active:
            self.stop()
        else:
            self.start(mode, auto_stop=True)

    def worker(self) -> None:
        w = self.cfg["whisper"]
        while True:
            kind, data, mode = self.jobs.get()
            try:
                if kind == "audio":
                    t0 = time.monotonic()
                    text = stt.transcribe(data, w["url"], self.cfg["language"],
                                          "" if mode == "dictate" else self.brain.prompt, w["timeout"])
                    self.log(f"whisper {time.monotonic() - t0:.2f} s")
                else:
                    text = data
                self.brain.handle(text, mode)
                if kind == "audio":
                    self.keep(data, text, mode)
            except stt.STTError as e:
                self.log(f"! {e}")
                self.desktop.osd(self.brain.msg("stt_error"), "dialog-error")
            except Exception:
                traceback.print_exc()

    def keep(self, wav: bytes, text: str, mode: str) -> None:
        """Stores the recording and its transcript, keeps only the newest N."""
        n = self.cfg["audio"]["keep_recordings"]
        if n <= 0 or mode == "dictate":               # dictation may be private – never stored
            return
        try:
            RECORDINGS.mkdir(parents=True, exist_ok=True)
            now = time.time()
            stem = RECORDINGS / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int(now * 1000) % 1000:03d}"
            stem.with_suffix(".wav").write_bytes(wav)
            stem.with_suffix(".txt").write_text(f"{mode}\t{text}\n", encoding="utf-8")
            for old in sorted(RECORDINGS.glob("*.wav"))[:-n]:
                old.unlink(missing_ok=True)
                old.with_suffix(".txt").unlink(missing_ok=True)
        except OSError as e:
            self.log(f"! recording not kept: {e}")

    # --- KDE integration ------------------------------------------------------------

    def register_hotkeys(self, bus: dbus.Bus) -> None:
        kga = dbus.Interface(bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"), "org.kde.KGlobalAccel")
        for mode in MODES:
            combo = self.cfg["hotkeys"].get(mode, "")
            aid = dbus.Array(["ultra-command", mode, "ultra-command", self.brain.msg(f"hotkey_{mode}")], signature="s")
            kga.doRegister(aid)
            default = dbus.Array([keys.qt_key(combo)] if combo else [], signature="i")
            kga.setShortcut(aid, default, dbus.UInt32(1))                 # IsDefault
            active = kga.setShortcut(aid, default, dbus.UInt32(2))        # SetPresent, keeps user changes
            if active:
                self.log(f"hotkey {mode}: {combo or 'set in System Settings'}")
        path = kga.getComponent("ultra-command")

        def pressed(_c, action, _ts):
            if action in MODES:
                self.start(action)

        def released(_c, action, _ts):
            if action in MODES and self.mode == action:
                self.stop()

        bus.add_signal_receiver(pressed, "globalShortcutPressed", "org.kde.kglobalaccel.Component", path=path)
        bus.add_signal_receiver(released, "globalShortcutReleased", "org.kde.kglobalaccel.Component", path=path)

    def load_kwin_script(self, bus: dbus.Bus) -> None:
        try:
            with resources.as_file(resources.files("ultracommand") / "data" / "active_window.js") as js:
                load_kwin_script(bus, str(js), KWIN_SCRIPT)
        except dbus.DBusException as e:
            self.log(f"KWin script not loaded ({e.get_dbus_message()}) – terminal detection off")


    def run(self) -> None:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        self.service = Service(self, bus)
        self.register_hotkeys(bus)
        self.load_kwin_script(bus)
        threading.Thread(target=self.worker, name="worker", daemon=True).start()
        threading.Thread(target=self._warm_up, name="warm-up", daemon=True).start()
        self.log(f"ultra-command ready: {len(self.catalog.commands)} commands, language {self.cfg['language']}")
        loop = GLib.MainLoop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
        try:
            loop.run()
        finally:
            self.recorder.stop()
            unload_kwin_script(bus, KWIN_SCRIPT)

    def _warm_up(self) -> None:
        try:
            self.brain.assistant.warm_up()
            self.log(f"LLM {self.cfg['llm']['model']} loaded")
        except Exception as e:
            self.log(f"LLM warm-up failed: {e}")


def main(cfg: dict | None = None) -> None:
    Daemon(cfg or config.load()).run()
