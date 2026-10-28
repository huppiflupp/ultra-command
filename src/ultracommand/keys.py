"""Key names → Linux evdev codes (for ydotool) and Qt key codes (for KDE hotkeys).

ydotool sends *physical* key codes, so the keyboard layout matters for letters
that move between layouts (German QWERTZ swaps Y and Z). `LAYOUT_SWAPS` fixes
the common cases; everything else is the same on most Latin layouts.
"""

from __future__ import annotations

_LETTERS = dict(zip("qwertyuiop", range(16, 26))) | dict(zip("asdfghjkl", range(30, 39))) \
    | dict(zip("zxcvbnm", range(44, 51)))

EVDEV: dict[str, int] = {
    **_LETTERS,
    **{str(d): 1 + d for d in range(1, 10)}, "0": 11,
    **{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
    **{f"f{n}": 170 + n for n in range(13, 25)},          # F13 = 183
    "esc": 1, "escape": 1, "tab": 15, "enter": 28, "return": 28, "space": 57,
    "backspace": 14, "delete": 111, "del": 111, "insert": 110,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "altgr": 100,
    "meta": 125, "super": 125, "win": 125, "rctrl": 97, "rshift": 54,
    "minus": 12, "equal": 13, "comma": 51, "dot": 52, "period": 52, "slash": 53,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "leftbrace": 26, "rightbrace": 27,
    "backslash": 43, "print": 99, "menu": 127, "pause": 119, "scrolllock": 70,
    "volumeup": 115, "volumedown": 114, "mute": 113,
    "playpause": 164, "nextsong": 163, "previoussong": 165,
}

LAYOUT_SWAPS: dict[str, dict[str, str]] = {
    "de": {"y": "z", "z": "y"},
    "ch": {"y": "z", "z": "y"},
    "at": {"y": "z", "z": "y"},
}


def evdev_code(name: str, layout: str = "us") -> int:
    name = name.strip().lower()
    name = LAYOUT_SWAPS.get(layout, {}).get(name, name)
    try:
        return EVDEV[name]
    except KeyError:
        raise ValueError(f"unknown key: {name!r}") from None


def ydotool_args(combo: str, layout: str = "us") -> list[str]:
    """"ctrl+shift+t" → ["29:1", "42:1", "20:1", "20:0", "42:0", "29:0"]."""
    codes = [evdev_code(k, layout) for k in combo.split("+") if k.strip()]
    if not codes:
        raise ValueError(f"empty key combo: {combo!r}")
    return [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]


# --- Qt key codes for KGlobalAccel -----------------------------------------

QT_MODIFIERS = {"shift": 0x02000000, "ctrl": 0x04000000, "control": 0x04000000,
                "alt": 0x08000000, "meta": 0x10000000, "super": 0x10000000, "win": 0x10000000}

QT_KEYS: dict[str, int] = {
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},    # Qt::Key_A = 0x41
    **{str(d): 0x30 + d for d in range(10)},
    **{f"f{n}": 0x01000030 + n - 1 for n in range(1, 36)},
    "space": 0x20, "esc": 0x01000000, "escape": 0x01000000, "tab": 0x01000001,
    "backspace": 0x01000003, "return": 0x01000004, "enter": 0x01000005,
    "insert": 0x01000006, "delete": 0x01000007, "pause": 0x01000008, "print": 0x01000009,
    "home": 0x01000010, "end": 0x01000011, "left": 0x01000012, "up": 0x01000013,
    "right": 0x01000014, "down": 0x01000015, "pageup": 0x01000016, "pagedown": 0x01000017,
    "scrolllock": 0x01000026, "menu": 0x01000055,
}


def qt_key(combo: str) -> int:
    """"Meta+F13" → Qt key code with modifier bits, as KGlobalAccel expects."""
    code = 0
    *mods, key = [p.strip().lower() for p in combo.split("+")]
    for m in mods:
        if m not in QT_MODIFIERS:
            raise ValueError(f"unknown modifier: {m!r}")
        code |= QT_MODIFIERS[m]
    if key not in QT_KEYS:
        raise ValueError(f"unknown hotkey: {key!r}")
    return code | QT_KEYS[key]
