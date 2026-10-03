#!/usr/bin/env python3
"""Replay a mic recording (the bridge's debug .wav, 16 kHz) into an OpenAI Realtime session with the
bridge's exact session config, paced to real time, and print what the server hears: VAD events,
input transcripts and the model's own understanding (it is asked to repeat the user verbatim).

    python rt_replay.py rec.wav [--nr far_field|near_field|none] [--threshold 0.3] [--burst 0.32]
"""
import argparse, asyncio, base64, json, os, sys, time
from pathlib import Path
import numpy as np, soundfile as sf, websockets
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
_src = (Path(__file__).parent.parent / "custom_components/rtbridge/bridge.py").read_text()
_ns = {"np": np}
exec(_src[_src.index("def _fir_lowpass"):_src.index("class Session:")], _ns)   # the bridge's resampler, without HA
resample_16k_to_24k = _ns["resample_16k_to_24k"]

ap = argparse.ArgumentParser()
ap.add_argument("wav"); ap.add_argument("--nr", default="far_field"); ap.add_argument("--threshold", type=float, default=0.3)
ap.add_argument("--silence", type=int, default=900); ap.add_argument("--prefix", type=int, default=400)
ap.add_argument("--model", default="gpt-realtime-2.1"); ap.add_argument("--lead", type=float, default=1.25,
                help="seconds of audio that arrive before the session is ready (only the last 0.32 s is sent, like the bridge)")
a = ap.parse_args()

pcm, rate = sf.read(a.wav, dtype="int16")
assert rate == 16000 and pcm.ndim == 1
chunks = [pcm[i:i + 512].tobytes() for i in range(0, len(pcm), 512)]

async def main():
    t0 = time.monotonic()
    def ts(): return f"{time.monotonic() - t0:6.2f}"
    async with websockets.connect(f"wss://api.openai.com/v1/realtime?model={a.model}",
                                  additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, max_size=None) as ws:
        audio_in = {"format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": {"type": "server_vad", "threshold": a.threshold, "prefix_padding_ms": a.prefix,
                                       "silence_duration_ms": a.silence, "create_response": True, "interrupt_response": True},
                    "transcription": {"model": "gpt-4o-transcribe", "language": "ru"}}
        if a.nr != "none":
            audio_in["noise_reduction"] = {"type": a.nr}
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "output_modalities": ["text"],
            "instructions": "Ты стенографист. Повтори дословно по-русски то, что сказал пользователь, и больше ничего.",
            "audio": {"input": audio_in}}}))

        async def sender():
            await asyncio.sleep(0.05)
            st = {}; pending = []
            for i, c in enumerate(chunks):
                p = resample_16k_to_24k(c, st)
                t = i * 0.032
                if t < a.lead:
                    pending.append(p); continue
                for q in pending[-10:]:
                    await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(q).decode()}))
                pending.clear()
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(p).decode()}))
                await asyncio.sleep(max(0, t0 + t - time.monotonic()))
            await asyncio.sleep(4)
        s = asyncio.create_task(sender())
        while not s.done():
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
            except asyncio.TimeoutError:
                continue
            t = m["type"]
            if t in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped", "input_audio_buffer.committed"):
                print(ts(), t, m.get("audio_start_ms", m.get("audio_end_ms", "")))
            elif t == "conversation.item.input_audio_transcription.completed":
                print(ts(), "USER :", m["transcript"].strip())
            elif t == "response.output_text.done" or t == "response.text.done":
                print(ts(), "MODEL:", m["text"].strip())
            elif t == "error":
                print(ts(), "ERROR", m["error"])
asyncio.run(main())
