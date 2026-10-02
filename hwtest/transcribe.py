"""Local whisper (faster-whisper) with word timestamps → per-second timeline.

Usable standalone:  python transcribe.py runs/<run>/session.wav [--whisper small] [--split 10.5]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

_MODEL_CACHE: dict = {}
RATE = 16000


@dataclass
class Word:
    start: float
    end: float
    word: str
    prob: float


def load_model(size: str):
    if size not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        _MODEL_CACHE[size] = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODEL_CACHE[size]


def load_audio(wav: str | Path) -> np.ndarray:
    """float32 mono 16 kHz, bypassing faster-whisper's PyAV path (version-fragile)."""
    import soundfile as sf
    audio, rate = sf.read(str(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != RATE:
        idx = (np.arange(int(len(audio) * RATE / rate)) * rate / RATE).astype(int)
        audio = audio[idx]
    return audio


def transcribe(audio: np.ndarray | str | Path, size: str = "large-v3-turbo",
               language: str | None = "ru", offset: float = 0.0, normalize: bool = True) -> list[Word]:
    """Transcribe one chunk. `language=None` = auto-detect. Word times get `offset` added.
    `normalize` peak-normalizes the chunk first — the Voice PE is ~25 dB quieter in the
    laptop mic than the laptop's own playback, and whisper silently drops quiet speech."""
    if not isinstance(audio, np.ndarray):
        audio = load_audio(audio)
    if len(audio) < RATE // 4:
        return []
    if normalize:
        peak = float(np.abs(audio).max())
        if peak > 1e-4:
            audio = audio / peak * 0.9
    model = load_model(size)
    segments, _info = model.transcribe(audio, language=language, word_timestamps=True,
                                       beam_size=5, vad_filter=True,
                                       vad_parameters={"min_silence_duration_ms": 300},
                                       condition_on_previous_text=False)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            words.append(Word(round(w.start + offset, 2), round(w.end + offset, 2),
                              w.word.strip(), round(w.probability, 2)))
    return words


def transcribe_windows(audio: np.ndarray, windows: list[tuple[float, float, str | None]],
                       size: str = "large-v3-turbo", offset: float = 0.0) -> list[Word]:
    """Transcribe [(start_s, end_s, language), ...] independently (each normalized) and merge."""
    out: list[Word] = []
    for start, end, lang in windows:
        chunk = audio[int(start * RATE): int(end * RATE) if end else None]
        out += transcribe(chunk, size, lang, offset=offset + start)
    return out


def per_second(words: list[Word], duration: float, events: list | None = None) -> list[str]:
    """One line per second: words that START in that second + any events in it.
    `events` = iterable of objects with .t (sec), .name, .detail (e.g. devlog.Event)."""
    n = int(duration) + 1
    rows = []
    for s in range(n):
        ws = " ".join(w.word for w in words if s <= w.start < s + 1)
        evs = [e for e in (events or []) if s <= e.t < s + 1]
        ev_txt = "   ".join(f"⟨{e.t:5.2f} {e.name}{(': ' + e.detail[:80]) if e.detail else ''}⟩" for e in evs)
        rows.append(f"{s:3d}s | {ws:<60} {ev_txt}".rstrip())
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--whisper", default="large-v3-turbo", help="faster-whisper model size (small/medium/large-v3-turbo)")
    ap.add_argument("--lang", default="auto", help="ru | en | auto")
    ap.add_argument("--split", type=float, default=None, help="transcribe [0,split) and [split,end) separately (normalized)")
    a = ap.parse_args()
    lang = None if a.lang == "auto" else a.lang
    audio = load_audio(a.wav)
    dur = len(audio) / RATE
    if a.split:
        words = transcribe_windows(audio, [(0, a.split, lang), (a.split, dur, lang)], a.whisper)
    else:
        words = transcribe(audio, a.whisper, lang)
    print("\n".join(per_second(words, dur)))
    print(json.dumps([asdict(w) for w in words], ensure_ascii=False))
