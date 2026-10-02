#!/usr/bin/env python3
"""Bidirectional realtime voice session with the OpenAI Realtime API from this laptop.

    https://developers.openai.com/api/docs/guides/realtime
    WS  wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1   (Authorization: Bearer …)
    audio in/out: pcm16 mono 24 kHz, base64 in JSON events; server-side VAD ends turns.

Two uses:
  * `python realtime_openai.py`  — talk to the model through the laptop mic/speakers (sanity
    check of key + audio path). Ctrl-C to stop.
  * import `RealtimeSession` from a test driver: the model plays the *human* in front of the
    Voice PE — it hears the колонка through the laptop mic and talks back through the laptop
    speakers, following `instructions` (e.g. "say the wake word, ask for the time, judge the
    answer"). Hook `on_event` to log every server event with timestamps.

Needs OPENAI_API_KEY (hwtest/.env). Model/voice via OPENAI_REALTIME_MODEL / OPENAI_REALTIME_VOICE.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
import sounddevice as sd
import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

RATE = 24000          # OpenAI Realtime pcm16 default rate
CHUNK_MS = 40


class RealtimeSession:
    def __init__(self, instructions: str, *, model: str | None = None, voice: str | None = None,
                 vad: dict | None = None, transcribe_input: bool = True,
                 on_event: Callable[[dict], Awaitable[None] | None] | None = None,
                 input_device=None, output_device=None, mic: bool = True, speaker: bool = True):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY not set in hwtest/.env")
        self.model = model or os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
        self.voice = voice or os.environ.get("OPENAI_REALTIME_VOICE", "marin")
        self.url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.instructions = instructions
        self.vad = vad or {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
                           "silence_duration_ms": 600, "create_response": True, "interrupt_response": True}
        self.transcribe_input = transcribe_input
        self.on_event = on_event
        self.use_mic, self.use_speaker = mic, speaker
        self.input_device, self.output_device = input_device, output_device
        self.ws = None
        self.t0 = time.monotonic()
        self._out_q: queue.Queue[np.ndarray] = queue.Queue()
        self._out_buf = np.zeros(0, np.int16)
        self._mic_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.transcript: list[tuple[float, str, str]] = []   # (t, who, text)

    # ---- audio plumbing (PortAudio callbacks run in their own threads) ----------------
    def _mic_cb(self, indata, frames, t, status):
        if self._loop:
            self._loop.call_soon_threadsafe(self._mic_q.put_nowait, bytes(indata))

    def _spk_cb(self, outdata, frames, t, status):
        while len(self._out_buf) < frames:
            try:
                self._out_buf = np.concatenate([self._out_buf, self._out_q.get_nowait()])
            except queue.Empty:
                break
        n = min(frames, len(self._out_buf))
        outdata[:n, 0] = self._out_buf[:n]
        outdata[n:, 0] = 0
        self._out_buf = self._out_buf[n:]

    def flush_playback(self):
        """Barge-in: drop queued model audio (server sent input_audio_buffer.speech_started)."""
        while not self._out_q.empty():
            try: self._out_q.get_nowait()
            except queue.Empty: break
        self._out_buf = np.zeros(0, np.int16)

    # ---- protocol ---------------------------------------------------------------------
    async def send(self, event: dict):
        await self.ws.send(json.dumps(event))

    async def send_audio(self, pcm16: bytes):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16).decode()})

    async def say_text(self, text: str):
        """Inject a user text turn and ask for a spoken response."""
        await self.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self.send({"type": "response.create"})

    async def update_instructions(self, instructions: str):
        await self.send({"type": "session.update", "session": {"type": "realtime", "instructions": instructions}})

    def log(self, msg):
        print(f"[{time.monotonic() - self.t0:7.2f}] {msg}", flush=True)

    async def _session_update(self):
        session = {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE}, "turn_detection": self.vad},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.voice},
            },
        }
        if self.transcribe_input:
            session["audio"]["input"]["transcription"] = {"model": "gpt-4o-mini-transcribe"}
        await self.send({"type": "session.update", "session": session})

    async def _pump_mic(self):
        while True:
            await self.send_audio(await self._mic_q.get())

    async def _recv(self):
        async for raw in self.ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t == "response.output_audio.delta":
                if self.use_speaker:
                    self._out_q.put(np.frombuffer(base64.b64decode(ev["delta"]), dtype=np.int16))
            elif t == "response.output_audio_transcript.done":
                self.transcript.append((time.monotonic() - self.t0, "model", ev.get("transcript", "")))
                self.log(f"MODEL: {ev.get('transcript', '')}")
            elif t == "conversation.item.input_audio_transcription.completed":
                self.transcript.append((time.monotonic() - self.t0, "mic", ev.get("transcript", "")))
                self.log(f"MIC:   {ev.get('transcript', '')}")
            elif t == "input_audio_buffer.speech_started":
                self.flush_playback(); self.log("speech_started (barge-in → flushed playback)")
            elif t in ("session.created", "session.updated", "input_audio_buffer.speech_stopped",
                       "response.created", "response.done"):
                self.log(t)
            elif t == "error":
                self.log(f"ERROR {json.dumps(ev.get('error'), ensure_ascii=False)}")
            if self.on_event:
                r = self.on_event(ev)
                if asyncio.iscoroutine(r):
                    await r

    async def run(self, duration: float | None = None, first_text: str | None = None):
        self._loop = asyncio.get_running_loop()
        streams = []
        if self.use_mic:
            streams.append(sd.InputStream(samplerate=RATE, channels=1, dtype="int16", device=self.input_device,
                                          blocksize=RATE * CHUNK_MS // 1000, callback=self._mic_cb))
        if self.use_speaker:
            streams.append(sd.OutputStream(samplerate=RATE, channels=1, dtype="int16", device=self.output_device,
                                           blocksize=RATE * CHUNK_MS // 1000, callback=self._spk_cb))
        async with websockets.connect(self.url, additional_headers=self.headers, max_size=None) as ws:
            self.ws = ws
            self.log(f"connected {self.model} voice={self.voice}")
            await self._session_update()
            for s in streams:
                s.start()
            tasks = [asyncio.create_task(self._recv())]
            if self.use_mic:
                tasks.append(asyncio.create_task(self._pump_mic()))
            if first_text:
                await self.say_text(first_text)
            try:
                if duration:
                    await asyncio.sleep(duration)
                else:
                    await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    t.cancel()
                for s in streams:
                    s.stop(); s.close()


async def _main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instructions", default="Ты говоришь по-русски, коротко и дружелюбно. Отвечай одним-двумя предложениями.")
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl-C)")
    ap.add_argument("--first", default=None, help="text turn to send right after connecting")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    in_dev = os.environ.get("HWTEST_INPUT_DEVICE") or None
    out_dev = os.environ.get("HWTEST_OUTPUT_DEVICE") or None
    s = RealtimeSession(a.instructions, model=a.model, voice=a.voice, input_device=in_dev, output_device=out_dev)
    try:
        await s.run(a.duration, a.first)
    except KeyboardInterrupt:
        pass
    print("\n--- transcript ---")
    for t, who, text in s.transcript:
        print(f"{t:7.2f} {who:5}: {text}")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
