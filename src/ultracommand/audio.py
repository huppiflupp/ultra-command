"""Microphone recording via pw-record (PipeWire), 16 kHz mono 16-bit – what Whisper wants."""

from __future__ import annotations

import array
import io
import math
import subprocess
import threading
import time
import wave
from typing import Callable

RATE = 16000
CHUNK = RATE // 10 * 2          # 100 ms of s16 mono


def rms(chunk: bytes) -> float:
    samples = array.array("h", chunk[: len(chunk) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class Recorder:
    """One recording at a time. start() → stop() returns the WAV bytes.

    With `on_silence`, the recording ends by itself after `silence_seconds`
    without speech (once speech was heard) – used by the toggle mode.
    """

    def __init__(self, max_seconds: float = 60, silence_seconds: float = 1.2, silence_level: float = 500):
        self.max_seconds = max_seconds
        self.silence_seconds = silence_seconds
        self.silence_level = silence_level
        self._proc: subprocess.Popen | None = None
        self._pcm = bytearray()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._proc is not None

    def start(self, on_silence: Callable[[], None] | None = None) -> None:
        with self._lock:
            if self._proc:
                return
            self._pcm = bytearray()
            self._proc = subprocess.Popen(
                ["pw-record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._reader = threading.Thread(target=self._read, args=(self._proc, on_silence), daemon=True)
            self._reader.start()

    def _read(self, proc: subprocess.Popen, on_silence: Callable[[], None] | None) -> None:
        begun, quiet_since, started = False, None, time.monotonic()
        while chunk := proc.stdout.read(CHUNK):
            self._pcm += chunk
            now = time.monotonic()
            if now - started > self.max_seconds:
                proc.terminate()
                if on_silence:
                    on_silence()
                break
            if on_silence is None:
                continue
            if rms(chunk) >= self.silence_level:
                begun, quiet_since = True, None
            elif begun:
                quiet_since = quiet_since or now
                if now - quiet_since >= self.silence_seconds:
                    on_silence()
                    break

    def stop(self) -> bytes:
        """Ends the recording and returns it as WAV (empty bytes if nothing was recorded)."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return b""
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._reader:
            self._reader.join(timeout=2)
        pcm = bytes(self._pcm)
        return to_wav(pcm) if len(pcm) >= RATE // 5 * 2 else b""    # < 0.2 s: a slip of the key
