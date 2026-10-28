"""Talking to the desktop: keys (ydotool), clipboard (Klipper), KWin, apps, OSD, notifications.

KDE Plasma on Wayland. Every call made from the worker thread uses its own
private D-Bus connection, the main loop keeps the shared one.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time

import dbus

from . import keys


class DesktopError(RuntimeError):
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
        # Set by the KWin script (see daemon.py) whenever the active window changes.
        self.window_class = ""
        self.window_caption = ""
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
