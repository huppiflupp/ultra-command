"""Speech to text via a whisper.cpp server (`/inference`)."""

from __future__ import annotations

import json
import urllib.request
import uuid


class STTError(RuntimeError):
    pass


def _multipart(fields: dict[str, str], wav: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(wav: bytes, url: str, language: str = "", prompt: str = "", timeout: float = 30) -> str:
    fields = {"response_format": "json", "temperature": "0"}
    if language:
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, ctype = _multipart(fields, wav)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except OSError as e:
        raise STTError(f"whisper server {url}: {e}") from e
    if "error" in data:
        raise STTError(f"whisper: {data['error']}")
    return " ".join(data.get("text", "").split())
