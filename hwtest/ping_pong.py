#!/usr/bin/env python3
"""Acoustic end-to-end test of the real Voice PE from this laptop.

The laptop plays the wake word + a command through its speakers, records its mic the
whole time, captures the device's ESPHome log over USB, then transcribes the recording
with local whisper and prints a per-second timeline with device events aligned.

    python ping_pong.py                               # «Солнце моё» … «Это пинг, ответь понг»
    python ping_pong.py --command "сколько времени" --expect ""   # no expectation, just look
    python ping_pong.py --tts elevenlabs --pause 0.6 --whisper small
    python ping_pong.py --list-devices

Outputs go to runs/<timestamp>/: session.wav, device.log, timeline.txt, report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from devlog import DeviceLog, Event  # noqa: E402
from tts import synth  # noqa: E402

REC_RATE = 16000


_TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i",
                           "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
                           "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
                           "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": ""})


def fold(s: str) -> str:
    """lowercase, ё→е, Cyrillic→Latin — so «понг» matches both «Понг» and whisper's «Pong»."""
    return s.lower().replace("ё", "е").translate(_TRANSLIT)


def words_of(s: str) -> set[str]:
    return set(re.sub(r"[^а-яёa-z0-9 ]+", " ", s.lower().replace("ё", "е")).split())


class Recorder:
    def __init__(self, device=None):
        self.chunks: list[np.ndarray] = []
        self.stream = sd.InputStream(samplerate=REC_RATE, channels=1, dtype="int16",
                                     device=device, callback=self._cb)
        self.t_start = None

    def _cb(self, indata, frames, t, status):
        if status:
            print(f"[rec] {status}", file=sys.stderr)
        self.chunks.append(indata.copy())

    def start(self):
        self.stream.start()
        self.t_start = time.monotonic()

    def stop(self) -> np.ndarray:
        self.stream.stop(); self.stream.close()
        return np.concatenate(self.chunks)[:, 0] if self.chunks else np.zeros(0, np.int16)


def play(audio: np.ndarray, rate: int, device=None, gain: float = 1.0):
    sd.play(audio * gain, rate, device=device)
    sd.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wake", default="Солнце моё")
    ap.add_argument("--command", default="Это пинг, ответь понг")
    ap.add_argument("--expect", default="понг", help="word the device's answer must contain ('' = none)")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between wake word and command")
    ap.add_argument("--lead", type=float, default=1.0, help="seconds of recording before the wake word")
    ap.add_argument("--listen", type=float, default=25.0, help="max seconds to wait for the answer after the command")
    ap.add_argument("--tts", choices=["say", "elevenlabs"], default="say")
    ap.add_argument("--voice", default=None, help="say voice name or ElevenLabs voice id")
    ap.add_argument("--whisper", default="large-v3-turbo", help="faster-whisper size: small | medium | large-v3-turbo")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--lang", default="ru", help="language hint for the device's answer: ru | en | auto "
                    "(ru still transcribes English error messages fine; auto mislabels short «понг, ня» as English)")
    ap.add_argument("--play-gain", type=float, default=0.7, help="volume multiplier for the laptop's own phrases")
    ap.add_argument("--serial", default=os.environ.get("VOICE_PE_SERIAL") or None, help="port, or 'off'")
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()

    if a.list_devices:
        print(sd.query_devices()); return

    in_dev = os.environ.get("HWTEST_INPUT_DEVICE") or None
    out_dev = os.environ.get("HWTEST_OUTPUT_DEVICE") or None
    if in_dev and in_dev.isdigit(): in_dev = int(in_dev)
    if out_dev and out_dev.isdigit(): out_dev = int(out_dev)

    run_dir = Path(a.out) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True)
    print(f"run dir: {run_dir}")

    # Synthesize up front so network/TTS time doesn't land inside the recording.
    wake_a, wake_r = synth(a.wake, a.tts, a.voice)
    cmd_a, cmd_r = synth(a.command, a.tts, a.voice)
    print(f"tts={a.tts}: wake {len(wake_a)/wake_r:.2f}s, command {len(cmd_a)/cmd_r:.2f}s")

    t0 = time.monotonic()
    harness: list[Event] = []
    def mark(name, detail=""):
        e = Event(round(time.monotonic() - t0, 3), name, detail); harness.append(e); print(f"  {e.t:6.2f}s  {name} {detail}")
        return e

    dev = None
    if a.serial != "off":
        try:
            dev = DeviceLog(a.serial, t0=t0).start()
            print(f"device log: {dev.port}")
        except Exception as e:
            print(f"device log: DISABLED ({e})")

    rec = Recorder(in_dev); rec.start()
    rec_offset = rec.t_start - t0   # recording sample 0 == this many seconds after t0
    mark("rec_start")
    time.sleep(a.lead)

    mark("play_wake", a.wake); play(wake_a, wake_r, out_dev, a.play_gain); mark("wake_end")
    time.sleep(a.pause)
    mark("play_cmd", a.command); play(cmd_a, cmd_r, out_dev, a.play_gain); t_cmd_end = mark("cmd_end").t

    # Listen until the device finished answering (per its log) or timeout.
    deadline = t_cmd_end + a.listen
    reason = "timeout"
    while time.monotonic() - t0 < deadline:
        time.sleep(0.2)
        if dev:
            ended = dev.has("pipeline_ended", after=t_cmd_end)
            if ended:
                idle = [e for e in dev.snapshot() if e.name == "player_state" and e.detail == "IDLE" and e.t > ended.t]
                if idle:
                    time.sleep(1.0); reason = "device finished"; break
            err = dev.has("response_text", after=t_cmd_end)
            if err and not dev.has("player_state", after=err.t) and time.monotonic() - t0 > err.t + 8:
                reason = "response without playback"; break
    mark("rec_stop", reason)

    audio = rec.stop()
    if dev: dev.stop()
    duration = len(audio) / REC_RATE
    sf.write(run_dir / "session.wav", audio, REC_RATE)
    if dev:
        dev.dump(run_dir / "device.log")
    dev_events = dev.snapshot() if dev else []
    all_events = sorted(harness + dev_events, key=lambda e: e.t)
    (run_dir / "events.json").write_text(json.dumps([asdict(e) for e in all_events], ensure_ascii=False, indent=1))

    # ---- transcript ---------------------------------------------------------------
    words = []
    if not a.no_whisper:
        from transcribe import load_audio, per_second, transcribe_windows
        print(f"\ntranscribing {duration:.1f}s with faster-whisper {a.whisper} …")
        t_w = time.monotonic()
        # Two windows on the recording clock: what we said (ru) / what the device answered
        # (auto language — error messages come back in English). Each is peak-normalized.
        split = max(0.0, t_cmd_end - rec_offset)
        lang_dev = None if a.lang == "auto" else a.lang
        words = transcribe_windows(load_audio(run_dir / "session.wav"),
                                   [(0.0, split, "ru"), (split, duration, lang_dev)],
                                   a.whisper, offset=rec_offset)   # → test clock
        print(f"  done in {time.monotonic() - t_w:.1f}s, {len(words)} words")
        rows = per_second(words, duration + rec_offset, all_events)
        timeline = "\n".join(rows)
        (run_dir / "timeline.txt").write_text(timeline)
        print("\n" + timeline)

    # ---- verdict ------------------------------------------------------------------
    print("\n=== RESULT ===")
    checks = {}
    def ev(name, after=-1):
        return next((e for e in dev_events if e.name == name and e.t >= after), None)
    t_wake = next(e.t for e in harness if e.name == "play_wake")
    wake = ev("wake_detected", t_wake)
    checks["wake_detected"] = bool(wake)
    print(f"wake word detected:      {'YES' if wake else 'NO'}" + (f"  (+{wake.t - t_wake:.2f}s after we started saying it)" if wake else ""))
    stt = ev("stt_text", t_cmd_end - 5)
    stt_ok = bool(stt) and words_of(a.command) <= words_of(stt.detail) | {"это"} and len(words_of(a.command) & words_of(stt.detail)) >= max(1, len(words_of(a.command)) - 1)
    checks["stt_matches"] = stt_ok
    print(f"device STT:              {stt.detail if stt else 'NONE'}  [{'ok' if stt_ok else 'MISMATCH'}]" + (f"  (+{stt.t - t_cmd_end:.2f}s after command)" if stt else ""))
    resp = ev("response_text", t_cmd_end)
    print(f"device response text:    {resp.detail[:120] if resp else 'NONE'}")
    play_ev = next((e for e in dev_events if e.name == "player_state" and e.detail == "ANNOUNCING" and e.t > t_cmd_end), None)
    if play_ev:
        print(f"device started speaking: +{play_ev.t - t_cmd_end:.2f}s after command")
    heard = [w for w in words if w.start > t_cmd_end]
    heard_txt = " ".join(w.word for w in heard)
    print(f"laptop heard after cmd:  {heard_txt or '(nothing)'}")
    if a.expect:
        exp = fold(a.expect)
        in_resp = bool(resp) and exp in fold(resp.detail)
        in_heard = exp in fold(heard_txt)
        checks["expect_in_response"] = in_resp
        checks["expect_heard"] = in_heard
        first = next((w for w in heard if exp in fold(w.word)), None)
        print(f"expected «{a.expect}»:      in response text: {'YES' if in_resp else 'NO'}; heard by mic: {'YES' if in_heard else 'NO'}"
              + (f" at +{first.start - t_cmd_end:.2f}s after command" if first else ""))
    ok = all(checks.values())
    print("VERDICT:", "PASS" if ok else "FAIL", json.dumps(checks))
    report = {"args": vars(a), "run_dir": str(run_dir), "duration_s": duration, "stop_reason": reason,
              "checks": checks, "pass": ok, "stt": stt.detail if stt else None,
              "response": resp.detail if resp else None, "heard_after_cmd": heard_txt,
              "words": [asdict(w) for w in words]}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
