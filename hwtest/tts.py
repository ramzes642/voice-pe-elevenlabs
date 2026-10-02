"""Phrase synthesis for the laptop "fake human".

Two backends, both cached in hwtest/cache/ so repeated runs are free and bit-identical:
  * say        — macOS built-in Russian voice (Milena). No key, offline. Default.
  * elevenlabs — ElevenLabs TTS REST (needs ELEVENLABS_API_KEY). Better prosody, more voices.

Returns float32 mono numpy audio + sample rate, ready for sounddevice.play().
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

CACHE = Path(__file__).parent / "cache"


def _cache_path(backend: str, voice: str, text: str) -> Path:
    key = hashlib.sha1(f"{backend}|{voice}|{text}".encode()).hexdigest()[:16]
    CACHE.mkdir(exist_ok=True)
    return CACHE / f"{backend}_{voice}_{key}.wav"


def synth(text: str, backend: str = "say", voice: str | None = None) -> tuple[np.ndarray, int]:
    if backend == "say":
        voice = voice or "Milena"
        path = _cache_path("say", voice, text)
        if not path.exists():
            aiff = path.with_suffix(".aiff")
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
            data, rate = sf.read(aiff, dtype="float32")
            aiff.unlink()
            sf.write(path, data, rate)
    elif backend == "elevenlabs":
        import httpx
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise SystemExit("ELEVENLABS_API_KEY not set in hwtest/.env (or use --tts say)")
        voice = voice or os.environ.get("ELEVENLABS_VOICE_ID") or "cjVigY5qzO86Huf0OWal"
        path = _cache_path("el", voice, text)
        if not path.exists():
            rate = 24000
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                           params={"output_format": f"pcm_{rate}"},
                           headers={"xi-api-key": key, "content-type": "application/json"},
                           json={"text": text, "model_id": "eleven_flash_v2_5",
                                 "language_code": "ru"},
                           timeout=60)
            r.raise_for_status()
            pcm = np.frombuffer(r.content, dtype=np.int16).astype(np.float32) / 32768.0
            sf.write(path, pcm, rate)
    else:
        raise SystemExit(f"unknown tts backend {backend!r}")
    data, rate = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, rate


if __name__ == "__main__":
    import sys
    import sounddevice as sd
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    backend = sys.argv[1] if len(sys.argv) > 1 else "say"
    text = " ".join(sys.argv[2:]) or "Солнце моё"
    a, r = synth(text, backend)
    print(f"{backend}: {len(a)/r:.2f}s @ {r} Hz")
    sd.play(a, r); sd.wait()
