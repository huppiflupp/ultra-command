"""The ultra-command daemon: hotkeys → recording → Whisper → command / dictation / assistant.

Runs a GLib main loop for D-Bus (KDE hotkeys, the KWin script, the control
interface). Recognition and actions run on a worker thread, one utterance
after the other.
"""

from __future__ import annotations

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
from .desktop import Desktop, DesktopError

BUS_NAME = "io.github.huppiflupp.UltraCommand"
OBJ_PATH = "/io/github/huppiflupp/UltraCommand"
KWIN_SCRIPT = "ultra-command-active-window"
MODES = ("talk", "dictate", "assistant")


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
        if self.catalog.is_noise(text):
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
        if fallback == "dictate":
            self.dictate(text)
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
        except DesktopError as e:
            self.log(f"  ! {e}")
            self.desktop.osd(self.msg("failed", what="type"), "dialog-error")

    def ask(self, text: str) -> None:
        self.desktop.osd(self.msg("thinking"), "dialog-question")
        try:
            answer = self.assistant.ask(text)
        except Exception as e:                      # network, model missing, bad JSON …
            self.log(f"  ! assistant: {e}")
            self.desktop.notify("ultra-command", self.msg("assistant_error", error=e), "dialog-error")
            return
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

    @dbus.service.method(BUS_NAME, in_signature="ss")
    def ActiveWindow(self, cls, caption):
        self.daemon.desktop.window_class, self.daemon.desktop.window_caption = str(cls), str(caption)


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.desktop = Desktop(cfg)
        self.catalog = load_catalog(cfg)
        self.brain = Brain(cfg, self.desktop, self.catalog, log=self.log)
        a = cfg["audio"]
        self.recorder = Recorder(a["max_seconds"], a["silence_seconds"], a["silence_level"])
        self.mode = "talk"
        self.jobs: queue.Queue = queue.Queue()
        self.prompt = self.catalog.vocabulary() if cfg["whisper"]["prompt_commands"] else ""

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
                                          "" if mode == "dictate" else self.prompt, w["timeout"])
                    self.log(f"whisper {time.monotonic() - t0:.2f} s")
                else:
                    text = data
                self.brain.handle(text, mode)
            except stt.STTError as e:
                self.log(f"! {e}")
                self.desktop.osd(self.brain.msg("stt_error"), "dialog-error")
            except Exception:
                traceback.print_exc()

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
            scripting = dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting")
            if scripting.isScriptLoaded(KWIN_SCRIPT):
                scripting.unloadScript(KWIN_SCRIPT)
            with resources.as_file(resources.files("ultracommand") / "data" / "active_window.js") as js:
                sid = scripting.loadScript(str(js), KWIN_SCRIPT, signature="ss")
            dbus.Interface(bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}"), "org.kde.kwin.Script").run()
        except dbus.DBusException as e:
            self.log(f"KWin script not loaded ({e.get_dbus_message()}) – terminal detection off")

    def unload_kwin_script(self, bus: dbus.Bus) -> None:
        try:
            dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting") \
                .unloadScript(KWIN_SCRIPT)
        except dbus.DBusException:
            pass

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
            self.unload_kwin_script(bus)

    def _warm_up(self) -> None:
        try:
            self.brain.assistant.warm_up()
            self.log(f"LLM {self.cfg['llm']['model']} loaded")
        except Exception as e:
            self.log(f"LLM warm-up failed: {e}")


def main(cfg: dict | None = None) -> None:
    Daemon(cfg or config.load()).run()
