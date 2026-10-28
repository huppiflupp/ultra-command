# Ultra-Command

Voice control for the Linux desktop – KDE Plasma on Wayland, fully local.

Hold a key, speak, let go. Ultra-Command decides what you meant:

1. **A command** from the catalog – *"maximize window"*, *"new tab"*, *"open file manager"*, *"down three"* → keys, KWin actions, programs.
2. **A keyword first** – *"Computer, …"* sends the rest to a **local LLM** that acts through the same commands; *"Type …"* dictates.
3. **Anything else** is shown to the LLM, which may pick *one* command for it – or nothing. It cannot act freely here, and a program name only counts if it was really said (*"Karte"* does not open Kate). This is `fallback = "guess"`; alternatives: `assistant` (acts freely), `dictate`, `ignore`. Dictation: start with *"Type …"* or use the dictate hotkey.

Speech recognition is [whisper.cpp](https://github.com/ggml-org/whisper.cpp) on the GPU (~50 ms per utterance with large-v3-turbo on an RTX 5080), the assistant runs on [Ollama](https://ollama.com). Nothing leaves the machine.

It was built for working with tendonitis – fewer keystrokes, fewer mouse moves – but it is not tied to one person: all language lives in data files (German and English included), and the commands are plain TOML.

## The assistant acts, and sees what happened

The catalog commands are offered to the LLM as tools, plus `type_text`, `get_clipboard`, `list_windows` and `focus_window`. Every tool call is executed and its result – or its error – goes back to the model, which then continues, tries something else or answers in one sentence (shown as a notification). This loop is what makes requests like these possible:

- *"Computer, put this window on the other screen and make it full screen"* – two tool calls in sequence.
- *"Computer, which windows are open?"* – the model asks `list_windows` and answers from the result.
- *"Computer, summarize what I copied"* – `get_clipboard`, then an answer.
- *"Computer, open the mail program"* – if a program is missing, the error comes back and the model can say so instead of pretending.

Follow-up questions within two minutes keep the conversation. The model can only use what is in the catalog – there is no free shell access unless you put a `shell` step into one of your own commands.

## How it fits together

```
 hold key (KGlobalAccel) ──► pw-record ──► whisper-server (GPU) ──► text
                                                                    │
           ┌──────────────── keyword? ── command? ── fallback ◄─────┘
           ▼                    ▼            ▼           ▼
     Ollama tool loop      executor:     executor     dictation
     (result → model)      ydotool, KWin, programs,  (clipboard + paste)
                           shell, clipboard
                                 │
              OSD / notification ◄┘   KWin script reports the active window
```

- **Hotkeys** are registered with KDE's KGlobalAccel, which reports *press* and *release* – that gives push-to-talk without root. Change the keys in *System Settings → Shortcuts → Ultra-Command*.
- **Keys** are sent with `ydotool` (uinput). `wtype` does not work on KWin.
- **Text** goes through the clipboard (Klipper) and a paste shortcut, so every character of every language works regardless of keyboard layout. Terminals get `Ctrl+Shift+V`; the previous clipboard content is restored.
- **Terminal-aware keys:** a small KWin script reports the active window. In a terminal, *"copy"* sends `Ctrl+Shift+C` instead of `Ctrl+C` (which would interrupt the running program).

## Requirements

- KDE Plasma 6 on Wayland (KGlobalAccel, KWin scripting, Klipper, Plasma OSD)
- PipeWire (`pw-record`), `ydotool` ≥ 1.0 with write access to `/dev/uinput`
- whisper.cpp server, ideally built with CUDA/ROCm/Vulkan
- Ollama with a model that supports tool calls (default `gemma4:12b-it-qat`, ~8 GB VRAM, 0.4 s per step when loaded)
- Python ≥ 3.11 with `dbus-python` and `PyGObject` from the distribution

## Installation

```bash
git clone https://github.com/huppiflupp/ultra-command ~/projects/ultra-command
cd ~/projects/ultra-command
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .

# uinput for ydotool, if your user cannot write /dev/uinput yet
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-uinput.rules && sudo usermod -aG input $USER   # then log in again

# services (adjust the whisper paths in ultra-command-whisper.service first)
ln -s "$PWD"/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ydotoold ultra-command-whisper ultra-command

.venv/bin/ultra-command check          # everything there?
```

## Usage

| | |
|---|---|
| hold `ScrollLock` | talk: command, keyword or fallback |
| `ultra-command toggle` | start/stop without holding; stops by itself after 1.2 s of silence – bind it to a key, a foot pedal or a macro pad |
| `ultra-command say "maximize window"` | let the running daemon handle text as if spoken |
| `ultra-command match "open files"` | dry run: show what a phrase would do, without doing it |
| `ultra-command commands` | list all commands and keywords |
| `journalctl --user -u ultra-command -f` | see what was recognized and what happened |

Optional extra hotkeys: `dictate` (always type) and `assistant` (always ask the LLM).

## Configuration

`~/.config/ultra-command/config.toml` – every key is optional, see [`config.example.toml`](config.example.toml) and the defaults in [`config.py`](src/ultracommand/config.py).

```toml
language = "en"

[hotkeys]
talk = "ScrollLock"
assistant = "Meta+F13"

[route]
fallback = "guess"       # or "assistant", "dictate", "ignore"

[llm]
model = "qwen3.5:9b"
```

## Commands

Built-in commands: [`commands-de.toml`](src/ultracommand/data/commands-de.toml), [`commands-en.toml`](src/ultracommand/data/commands-en.toml). Your own go into `~/.config/ultra-command/commands.toml`; a command with the same `id` replaces a built-in one.

```toml
[slots.room]
kitchen = "light.kitchen"
office = "light.office"

[[command]]
id = "light.on"
say = ["light on in the {room}", "{room} light on"]
do = { shell = "hass-cli state turn_on {room}" }
help = "Switch a light on"            # the LLM sees this

[[command]]
id = "terminal.clear"
say = ["clear screen"]
do = [{ type = "clear" }, { key = "enter" }]
llm = false                           # not offered to the assistant
```

Steps: `key` (with `repeat`, `terminal`), `type`, `run`, `focus` (switch to the program's window, cycle if it is active, start it if not), `minimize` (the program's window), `kwin` (KWin shortcut name), `shortcut` (`component/name` of any global shortcut), `shell` (output goes back to the assistant), `sleep`, `builtin` (`repeat`, `cancel`).
Slots: `{n}` is a number (digits or number words), `{text}` free text, anything else comes from `[slots.<name>]` and is matched fuzzily. Phrases match after removing punctuation and filler words; small recognition errors are tolerated (`route.fuzzy`).

### Another language

Copy `commands-en.toml` to `commands-<code>.toml`, translate `say`, `keywords`, `numbers`, `filler`, `ignore` and `[messages]`, and set `language = "<code>"`. Nothing in the code is language-specific. Pull requests welcome.

## Status and ideas

Working: push-to-talk, commands with slots, window switching by voice, dictation, the assistant tool loop, terminal-aware keys, OSD/notification feedback.

To tune recognition with your own voice and microphone, set `audio.keep_recordings = 50`: the last recordings and their transcripts are then kept in `~/.cache/ultra-command/recordings/` (never in dictation mode). Off by default.

Next:
- **Eye tracking with a webcam** – focus the window you look at (left/right screen, quadrants), so *"copy"* goes where your eyes are.
- Mouse grid by voice (click into numbered screen regions).
- Keep the microphone stream open for a pre-roll buffer.
- Optional confirmation for destructive commands proposed by the assistant.

## License

MIT
