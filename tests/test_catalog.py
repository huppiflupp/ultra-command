import copy
import re

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


@pytest.mark.parametrize("text", ["Wechsel zum Dateimanager", "wechseln, zu, dolphin,", "Geh zum Dateimanager",
                                  "Dateimanager", "nächstes Dolphin"])
def test_focus(de, text):
    assert ids(de, text) == ("window.focus", {"app": "dolphin"})


def test_open_beats_bare_app(de):
    assert ids(de, "Öffne Thunderbird") == ("app.open", {"app": "thunderbird"})
    assert ids(de, "Thunderbird öffnen") == ("app.open", {"app": "thunderbird"})


@pytest.mark.parametrize("text", ["Karte", "Kante", "Datei", "Rechnen", "Kein Mail"])
def test_bare_slot_needs_exact_value(de, text):
    m = de.match(text)
    assert m is None or m.command.id != "window.focus"


def test_text_slot_keeps_words():
    cat = Catalog()
    cat.add_toml(config.builtin_commands("de"))
    cat.add_toml('[[command]]\nid = "note"\nsay = ["notiz {text}"]\ndo = {type = "{text}"}')
    assert ids(cat, "Notiz zum Bahnhof gehen") == ("note", {"text": "zum bahnhof gehen"})


def test_windows_of():
    from ultracommand.desktop import Desktop
    d = Desktop(copy.deepcopy(config.DEFAULTS))
    d.windows = [{"id": "1", "cls": "org.kde.konsole", "caption": "", "active": False},
                 {"id": "2", "cls": "net.thunderbird.Thunderbird", "caption": "", "active": True},
                 {"id": "3", "cls": "code-oss-helper", "caption": "", "active": False},
                 {"id": "4", "cls": "org.foo.App", "caption": "", "active": False}]
    assert [w["id"] for w in d.windows_of("konsole")] == ["1"]
    assert [w["id"] for w in d.windows_of("thunderbird")] == ["2"]
    assert d.windows_of("code") == []                      # no substring match
    assert [w["id"] for w in d.windows_of("flatpak run org.foo.App")] == ["4"]


@pytest.mark.parametrize("text, answer, expected", [
    ("Karte", {"command": "app.open", "args": {"app": "kate"}}, None),            # name not said
    ("Mail Programm aufmachen", {"command": "app.open", "args": {"app": "thunderbird"}},
     ("app.open", {"app": "thunderbird"})),                                       # spoken key "mail"
    ("Bring Thunderbird nach vorne", {"command": "window.focus", "args": {"app": "thunderbird"}},
     ("window.focus", {"app": "thunderbird"})),
    ("ein bisschen runter", {"command": "move.down", "args": {"n": "2"}}, ("move.down", {"n": 1})),  # n not said
    ("drei runter bitte", {"command": "move.down", "args": {"n": "50"}}, ("move.down", {"n": 3})),
    ("öffne reboot", {"command": "app.open", "args": {"app": "reboot"}}, None),        # not a slot value
    ("öffne kate", {"command": "uc.unknown", "args": {}}, None),
    ("bla", {"command": "uc.repeat", "args": {}}, None),                          # not offered to the LLM
    ("bla", {"command": None, "args": {}}, None),
])
def test_classify_guard(de, text, answer, expected):
    import json
    from ultracommand.assistant import Assistant
    a = Assistant(copy.deepcopy(config.DEFAULTS), de, executor=None, desktop=None)
    a._chat = lambda *args, **kw: {"message": {"content": json.dumps(answer)}}
    m = a.classify(text)
    assert ((m.command.id, m.slots) if m else None) == expected


def test_prompt_echo():
    p = "Wechsel zu Thunderbird. Öffne Dolphin. Neuer Tab. Arbeitsfläche 2."
    cat = Catalog()
    assert cat.is_prompt_echo("Wechsel zu Thunderbird. Öffne Dolphin.", p)
    assert not cat.is_prompt_echo("Wechsel zu Thunderbird.", p)          # can be a real command
    assert not cat.is_prompt_echo("Wechsel zu Thunderbird. Öffne Dolphin.", "")  # prompt off
    # whole words only, and the sentences must follow each other
    assert not cat.is_prompt_echo("dann neuer tabellenreiter arbeitsfläche 2", p)
    assert not cat.is_prompt_echo("Öffne Dolphin und dann wechsel zu thunderbird", p)


@pytest.mark.parametrize("lang", ["de", "en"])
def test_shipped_prompt_is_no_command(lang):
    cat = Catalog()
    cat.add_toml(config.builtin_commands(lang))
    assert cat.prompt and cat.is_prompt_echo(cat.prompt, cat.prompt)
    for sentence in re.split(r"[.!?]", cat.prompt):
        assert not sentence.strip() or (cat.match(sentence) is None and cat.keyword(sentence) is None)


def test_silence_is_dropped():
    from ultracommand.audio import Recorder
    r = Recorder(speech_level=400)
    r._proc, r._reader = None, None
    for loudest, expected in ((250.0, b""), (900.0, "wav")):
        r._pcm, r._loudest = bytearray(16000 * 2), loudest
        r._proc = type("P", (), {"terminate": lambda s: None, "wait": lambda s, timeout=None: 0})()
        out = r.stop()
        assert (out[:4] == b"RIFF") if expected == "wav" else out == b""


def test_guess_never_picks_destructive(de):
    import json
    from ultracommand.assistant import Assistant
    a = Assistant(copy.deepcopy(config.DEFAULTS), de, executor=None, desktop=None)
    seen = {}
    def chat(messages, **kw):
        seen["ids"] = kw["schema"]["properties"]["command"]["enum"]
        return {"message": {"content": json.dumps({"command": "window.close", "args": {}})}}
    a._chat = chat
    assert a.classify("mach das zu") is None
    assert "window.close" not in seen["ids"] and "key.enter" not in seen["ids"]


def test_guess_number_ambiguous(de):
    import json
    from ultracommand.assistant import Assistant
    a = Assistant(copy.deepcopy(config.DEFAULTS), de, executor=None, desktop=None)
    a._chat = lambda *x, **kw: {"message": {"content": json.dumps({"command": "move.down", "args": {}})}}
    assert a.classify("auf desktop 2 drei runter").slots == {"n": 1}
    assert a.classify("hm ² runter").slots == {"n": 2}          # NFKC: "²" → "2", no crash


def test_guess_target_number_must_be_said(de):
    import json
    from ultracommand.assistant import Assistant
    a = Assistant(copy.deepcopy(config.DEFAULTS), de, executor=None, desktop=None)
    a._chat = lambda *x, **kw: {"message": {"content": json.dumps({"command": "desktop.switch", "args": {"n": 1}})}}
    assert a.classify("nächste arbeitsfläche") is None              # target missing – no guess
    assert a.classify("geh mal auf die arbeitsfläche zwei").slots == {"n": 2}


def test_prompt_echo_single_sentence(de):
    assert de.is_prompt_echo("Thunderbird, Dolphin, Kate.", "Thunderbird, Dolphin, Kate")
    assert not de.is_prompt_echo("Thunderbird", "Thunderbird, Dolphin, Kate")


def test_too_short_resets_loudest():
    from ultracommand.audio import Recorder
    r = Recorder(speech_level=400)
    r.loudest = 3000.0
    r._pcm, r._loudest = bytearray(100), 3000.0
    r._proc = type("P", (), {"terminate": lambda s: None, "wait": lambda s, timeout=None: 0})()
    assert r.stop() == b"" and r.loudest == 0.0


def test_model_tag():
    from ultracommand.assistant import _with_tag
    assert _with_tag("qwen3.5") == "qwen3.5:latest"
    assert _with_tag("gemma4:12b-it-qat") == "gemma4:12b-it-qat"


def test_old_prompt_key_is_honored(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[whisper]\nprompt_commands = false\n")
    assert config.load(f)["whisper"]["prompt"] == "off"
