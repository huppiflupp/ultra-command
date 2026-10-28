import pytest

from ultracommand import config
from ultracommand.catalog import Catalog, normalize
from ultracommand.keys import qt_key, ydotool_args


@pytest.fixture(scope="module")
def de():
    cat = Catalog()
    cat.add_toml(config.builtin_commands("de"))
    return cat


@pytest.fixture(scope="module")
def en():
    cat = Catalog()
    cat.add_toml(config.builtin_commands("en"))
    return cat


def ids(cat, text):
    m = cat.match(text)
    return (m.command.id, m.slots) if m else None


def test_normalize():
    assert normalize("Fenster maximieren!") == "fenster maximieren"
    assert normalize("E-Mail öffnen.") == "e mail öffnen"


@pytest.mark.parametrize("text, expected", [
    ("Fenster maximieren.", ("window.maximize", {})),
    ("Bitte Fenster maximieren", ("window.maximize", {})),       # filler word
    ("Fenster maximiren", ("window.maximize", {})),              # small recognition error
    ("Neuer Tab.", ("tab.new", {})),
    ("Öffne den Dateimanager", ("app.open", {"app": "dolphin"})),  # article = filler
    ("Öffne Dateimanager", ("app.open", {"app": "dolphin"})),
    ("Starte E-Mail", ("app.open", {"app": "thunderbird"})),
    ("Runter drei", ("move.down", {"n": 3})),
    ("Runter 5", ("move.down", {"n": 5})),
    ("Desktop 2", ("desktop.switch", {"n": 2})),
    ("Ich gehe heute einkaufen.", None),
])
def test_match_de(de, text, expected):
    assert ids(de, text) == expected


def test_match_en(en):
    assert ids(en, "Maximize window.") == ("window.maximize", {})
    assert ids(en, "Open file manager") == ("app.open", {"app": "dolphin"})
    assert ids(en, "down three") == ("move.down", {"n": 3})


def test_keywords(de):
    assert de.keyword("Computer, mach das Fenster größer.") == ("assistant", "mach das Fenster größer.")
    assert de.keyword("Lokale KI: Was ist 2+2?") == ("assistant", "Was ist 2+2?")
    assert de.keyword("KI lokal öffne Firefox") == ("assistant", "öffne Firefox")
    assert de.keyword("Schreib Hallo Welt!") == ("dictate", "Hallo Welt!")
    assert de.keyword("Kino heute?") is None                   # "ki" only as a whole word


def test_noise(de):
    assert de.is_noise("Untertitel im Auftrag des ZDF für funk, 2017")
    assert de.is_noise("  ")
    assert not de.is_noise("Fenster maximieren")


def test_tools(de):
    tools = {t["function"]["name"]: t["function"] for t in de.tools()}
    assert "uc_repeat" not in tools                              # llm = false
    assert "dolphin" in tools["app_open"]["parameters"]["properties"]["app"]["enum"]
    assert tools["move_down"]["parameters"]["properties"]["n"]["type"] == "integer"
    assert de.by_tool("window_maximize").id == "window.maximize"


def test_override():
    cat = Catalog()
    cat.add_toml(config.builtin_commands("de"))
    cat.add_toml('[[command]]\nid = "tab.new"\nsay = ["mach tab"]\ndo = {key = "ctrl+n"}')
    assert ids(cat, "mach tab") == ("tab.new", {})
    assert ids(cat, "neuer tab") is None


def test_keys():
    assert ydotool_args("ctrl+t") == ["29:1", "20:1", "20:0", "29:0"]
    assert ydotool_args("ctrl+z", "de") == ydotool_args("ctrl+y", "us")    # QWERTZ
    assert qt_key("ScrollLock") == 0x01000026
    assert qt_key("Meta+F13") == 0x10000000 | 0x0100003C


def test_keyword_rest_keeps_original_text(de):
    assert de.keyword("- Schreib Hallo Welt.") == ("dictate", "Hallo Welt.")
    assert de.keyword("KI-Frage: wie spät?") == ("assistant", "Frage: wie spät?")
    assert de.keyword("Computer.") == ("assistant", "")


def test_say_as_string():
    cat = Catalog()
    cat.add_toml('[[command]]\nid = "x"\nsay = "öffne spotify"\ndo = {run = "spotify"}')
    assert cat.commands["x"].say == ["öffne spotify"]


def test_load_does_not_touch_defaults(tmp_path):
    (tmp_path / "c.toml").write_text('language = "en"')
    config.load(tmp_path / "missing.toml")
    assert config.DEFAULTS["input"]["layout"] == ""
    assert config.load(tmp_path / "c.toml")["input"]["layout"] == "en"


def test_repeat_is_capped(de):
    from ultracommand.actions import ActionError, Executor

    class FakeDesktop:
        def is_terminal(self):
            return False

        def key(self, combo, repeat):
            raise AssertionError("must not send 1000 key presses")

    m = de.match("runter 1000")
    with pytest.raises(ActionError):
        Executor(FakeDesktop()).execute(m.command, m.slots)
