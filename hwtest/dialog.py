#!/usr/bin/env python3
"""Scripted multi-turn dialog with the real Voice PE from the laptop.

    python dialog.py "Солнце моё" 1.5 "Привет, расскажи почему небо голубое" 6 "Стоп. Сколько будет пять плюс пять?" 12

Arguments alternate: phrase, seconds-to-wait-after-it. The mic records throughout, the device
log is captured over USB, and the recording is transcribed per window (laptop phrase window /
device answer window) so you can see what the колонка said and when.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import soundfile as sf
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
from devlog import DeviceLog, Event  # noqa: E402
from ping_pong import REC_RATE, Recorder, play  # noqa: E402
from transcribe import load_audio, per_second, transcribe_windows  # noqa: E402
from tts import synth  # noqa: E402


def main():
    args = sys.argv[1:]
    steps = [(args[i], float(args[i + 1]) if i + 1 < len(args) else 3.0) for i in range(0, len(args), 2)]
    run_dir = HERE / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_dialog")
    run_dir.mkdir(parents=True)
    audio = [synth(p) for p, _ in steps]
    t0 = time.monotonic()
    harness: list[Event] = []
    def mark(name, detail=""):
        e = Event(round(time.monotonic() - t0, 3), name, detail); harness.append(e); print(f"  {e.t:6.2f}s  {name} {detail}"); return e
    dev = None
    try:
        dev = DeviceLog(None, t0=t0).start()
    except Exception as e:
        print("device log disabled:", e)
    rec = Recorder(); rec.start(); rec_offset = rec.t_start - t0
    time.sleep(0.8)
    windows = []   # (start, end, lang) on the recording clock
    for (phrase, wait), (a, r) in zip(steps, audio):
        s = mark("say", phrase); play(a * 0.7, r); e = mark("said")
        windows.append((s.t - rec_offset, e.t - rec_offset, "ru"))
        t_end = time.monotonic() - t0 + wait
        windows.append((e.t - rec_offset, t_end - rec_offset, "ru"))
        while time.monotonic() - t0 < t_end:
            time.sleep(0.1)
    mark("rec_stop")
    pcm = rec.stop()
    if dev: dev.stop(); dev.dump(run_dir / "device.log")
    sf.write(run_dir / "session.wav", pcm, REC_RATE)
    dur = len(pcm) / REC_RATE
    print(f"\ntranscribing {dur:.1f}s …")
    words = transcribe_windows(load_audio(run_dir / "session.wav"), [(max(0, a), min(dur, b), l) for a, b, l in windows], offset=rec_offset)
    events = sorted(harness + ([e for e in dev.snapshot() if e.name != "error"] if dev else []), key=lambda e: e.t)
    rows = per_second(words, dur + rec_offset, events)
    (run_dir / "timeline.txt").write_text("\n".join(rows))
    print("\n".join(r[:230] for r in rows))
    print("run dir:", run_dir)


if __name__ == "__main__":
    main()
