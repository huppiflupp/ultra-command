"""Talking to the desktop: keys (ydotool), clipboard (Klipper), KWin, apps, OSD, notifications.

KDE Plasma on Wayland. Every call made from the worker thread uses its own
private D-Bus connection, the main loop keeps the shared one.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

import dbus

from . import keys


class DesktopError(RuntimeError):
    pass


def load_kwin_script(bus: dbus.Bus, path: str, name: str) -> None:
    """(Re)loads a KWin script under `name` and runs it."""
    scripting = dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting")
    if scripting.isScriptLoaded(name):
        scripting.unloadScript(name)
    sid = scripting.loadScript(path, name, signature="ss")      # overloaded: pick (path, name)
    dbus.Interface(bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}"), "org.kde.kwin.Script").run()


def unload_kwin_script(bus: dbus.Bus, name: str) -> None:
    try:
        dbus.Interface(bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting").unloadScript(name)
    except dbus.DBusException:
        pass


class Desktop:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        inp = cfg["input"]
        self.layout = inp["layout"]
        self.env = dict(os.environ)
        self.env["YDOTOOL_SOCKET"] = inp["ydotool_socket"] or os.environ.get(
            "YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
        self._bus: dbus.Bus | None = None
        self._notify_id = 0
        # Reported by the KWin script (data/active_window.js) on every window change:
        # [{"id", "cls", "caption", "active", "minimized", "here"}], stacking order bottom → top;
        # "here" = on the current virtual desktop.
        self.windows: list[dict] = []
        self._lock = threading.Lock()

    @property
    def bus(self) -> dbus.Bus:
        if self._bus is None:
            self._bus = dbus.SessionBus(private=True)
        return self._bus

    def _call(self, service: str, path: str, iface: str, method: str, *args):
        try:
            obj = self.bus.get_object(service, path)
            return getattr(dbus.Interface(obj, iface), method)(*args)
        except dbus.DBusException as e:
            raise DesktopError(f"{service} {method}: {e.get_dbus_message()}") from e

    # --- keyboard -------------------------------------------------------------

    def key(self, combo: str, repeat: int = 1) -> None:
        once = keys.ydotool_args(combo, self.layout)
        try:
            res = subprocess.run(["ydotool", "key", "-d", "8", *once * max(1, repeat)], env=self.env,
                                 capture_output=True, text=True, timeout=5 + 0.05 * len(once) * repeat)
        except subprocess.TimeoutExpired:
            # Killed mid-sequence: release everything, or Ctrl/Shift stay pressed.
            subprocess.run(["ydotool", "key", *[a for a in once if a.endswith(":0")]],
                           env=self.env, capture_output=True, timeout=5)
            raise DesktopError(f"ydotool timed out on {combo!r}") from None
        except OSError as e:
            raise DesktopError(f"ydotool: {e}") from e
        if res.returncode != 0 or "failed to connect" in res.stderr.lower():
            raise DesktopError(f"ydotool: {res.stderr.strip() or res.returncode} (is ydotoold running?)")

    def set_windows(self, data: str) -> None:
        self.windows = json.loads(data)

    @property
    def active_window(self) -> dict:
        return next((w for w in self.windows if w["active"]), {"id": "", "cls": "", "caption": ""})

    @property
    def window_class(self) -> str:
        return self.active_window["cls"]

    @property
    def window_caption(self) -> str:
        return self.active_window["caption"]

    def is_terminal(self) -> bool:
        return self.window_class.lower() in {t.lower() for t in self.cfg["input"]["terminals"]}

    def type_text(self, text: str) -> None:
        """Types text via the clipboard – works for every character and layout.

        Klipper only hands out text: a copied image or file cannot be restored
        afterwards (the dictated text stays in the clipboard instead).
        """
        if not text:
            return
        inp = self.cfg["input"]
        with self._lock:
            old = self.clipboard() if inp["restore_clipboard"] else ""
            self.set_clipboard(text)
            try:
                time.sleep(0.05)
                self.key(inp["terminal_paste_key"] if self.is_terminal() else inp["paste_key"])
                time.sleep(0.3)          # the application reads the clipboard asynchronously
            finally:
                if old:
                    self.set_clipboard(old)

    # --- clipboard (Klipper) -----------------------------------------------------

    def clipboard(self) -> str:
        return str(self._call("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", "getClipboardContents"))

    def set_clipboard(self, text: str) -> None:
        self._call("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", "setClipboardContents", text)

    # --- KWin / global shortcuts -----------------------------------------------

    def shortcut(self, component: str, name: str) -> None:
        """Triggers any global shortcut, e.g. ("kwin", "Window Maximize")."""
        path = f"/component/{component}"
        names = self._call("org.kde.kglobalaccel", path, "org.kde.kglobalaccel.Component", "shortcutNames")
        if name not in names:
            raise DesktopError(f"no global shortcut {name!r} in {component}")
        self._call("org.kde.kglobalaccel", path, "org.kde.kglobalaccel.Component", "invokeShortcut", name)

    # --- windows ---------------------------------------------------------------------

    def _on_window(self, window_id: str, action_js: str, done) -> bool:
        """Runs `action_js` (with `w` bound) on one window via a one-shot KWin script, then waits
        until the window script reports `done(window)` – or 1.5 s. Returns whether it did."""
        js = ("for (const w of workspace.windowList()) {\n"
              f"  if (w.internalId.toString() === {json.dumps(window_id)}) {{ {action_js} }}\n}}\n")
        name = "ultra-command-oneshot"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        try:
            load_kwin_script(self.bus, f.name, name)
            # KWin runs the script asynchronously; the window script reports the result.
            deadline = time.monotonic() + 1.5
            while not done(self._window(window_id)) and time.monotonic() < deadline:
                time.sleep(0.05)
        except dbus.DBusException as e:
            raise DesktopError(f"KWin: {e.get_dbus_message()}") from e
        finally:
            unload_kwin_script(self.bus, name)
            os.unlink(f.name)
        return done(self._window(window_id))

    def _window(self, window_id: str) -> dict:
        return next((w for w in self.windows if w["id"] == window_id), {})

    def activate(self, window_id: str) -> None:
        """Activates a window by its KWin id (un-minimizing it)."""
        if not self._on_window(window_id, "if (w.minimized) w.minimized = false; workspace.activeWindow = w;",
                               lambda w: w.get("active", False)):
            raise DesktopError(f"window {window_id} was not activated (closed?)")

    def minimize(self, app: str) -> str:
        """Minimizes the program's window: the active one if it is active, else the topmost visible."""
        wins = [w for w in self.windows_of(app) if not w.get("minimized")]
        wins = [w for w in wins if w.get("here", True)] or wins       # this desktop first
        if not wins:
            raise DesktopError(f"{app} has no visible window")
        target = next((w for w in wins if w["active"]), wins[-1])
        if not self._on_window(target["id"], "w.minimized = true;", lambda w: w.get("minimized", False)):
            raise DesktopError(f"{target['caption']!r} was not minimized")
        return f"minimized {target['caption']!r}"

    def windows_of(self, app: str) -> list[dict]:
        """Windows whose class belongs to a program ("konsole" → "org.kde.konsole").

        For "flatpak run org.foo.App" the app id is used. The class must be the
        program name itself or end in ".<name>" – no substring matches.
        """
        argv = shlex.split(app)
        if not argv:
            return []
        name = next((a for a in reversed(argv) if "." in a), argv[0]) if argv[0] == "flatpak" else argv[0]
        name = os.path.basename(name).lower()
        short = name.rsplit(".", 1)[-1]
        return [w for w in self.windows
                if w["cls"].lower() in (name, short) or w["cls"].lower().rsplit(".", 1)[-1] == short]

    def focus(self, app: str) -> str:
        """Switches to the program's window; cycles if it is already active; starts it if it has none."""
        if not self.windows:
            # Empty list = the KWin script has not reported (yet) – starting would duplicate windows.
            raise DesktopError("window list unknown (KWin script not loaded?)")
        wins = self.windows_of(app)
        if not wins:
            self.run(app)
            return f"{app} was not running, started it"
        current = next((i for i, w in enumerate(wins) if w["active"]), None)
        target = wins[(current + 1) % len(wins)] if current is not None else wins[-1]
        self.activate(target["id"])
        return f"activated {target['caption']!r}"

    # --- programs ------------------------------------------------------------------

    def run(self, command: str | list[str]) -> None:
        """Starts a program detached, in its own scope – it survives a restart of ultra-command."""
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or not shutil.which(argv[0]):
            raise DesktopError(f"program not found: {argv[0] if argv else command!r}")
        prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--"] \
            if shutil.which("systemd-run") else []
        subprocess.Popen(prefix + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=self.env)

    def shell(self, command: str, timeout: float = 15) -> str:
        """Runs a shell command and returns its output (fed back to the assistant)."""
        try:
            res = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                                 timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise DesktopError(f"timeout after {timeout} s: {command}") from None
        out = (res.stdout + res.stderr).strip()
        if res.returncode != 0:
            raise DesktopError(f"exit {res.returncode}: {out[-500:]}")
        return out[-2000:]

    # --- feedback ----------------------------------------------------------------------

    def osd(self, text: str, icon: str = "audio-input-microphone") -> None:
        if not self.cfg["feedback"]["osd"]:
            return
        try:
            self._call("org.kde.plasmashell", "/org/kde/osdService", "org.kde.osdService", "showText", icon, text)
        except DesktopError:
            pass

    def notify(self, title: str, body: str, icon: str = "audio-input-microphone", seconds: float | None = None) -> None:
        seconds = self.cfg["feedback"]["notify_seconds"] if seconds is None else seconds
        try:
            self._notify_id = int(self._call(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                "Ultra-Command", dbus.UInt32(self._notify_id), icon, title, body,
                dbus.Array([], signature="s"), dbus.Dictionary({}, signature="sv"), int(seconds * 1000)))
        except DesktopError:
            pass
