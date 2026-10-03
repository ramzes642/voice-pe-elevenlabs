#!/usr/bin/env python3
"""rtbridge — wake word on the Voice PE → live full-duplex conversation with OpenAI Realtime.

    python -m rtbridge.main --mode openai      # the real thing
    python -m rtbridge.main --mode loopback    # stage 1: echo what the device hears back to it

Flow: «солнце моё» → device `voice_assistant.start` → we accept (API audio) → mic 16 kHz
streams in continuously → resampled to 24 kHz → OpenAI; model audio → chunked WAV over
HTTP → device media player (announcement). Barge-in: OpenAI's server VAD cancels the
response, we stop the device's playback. The model ends the session with the
`end_conversation` tool; the device ends it with the wake word / "stop"; we end it on idle.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from .audio_http import AudioHTTP, Stream
from .device import VoicePE, psk_from_yaml

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
log = logging.getLogger("rtbridge")

MIC_RATE = 16000
OAI_RATE = 24000
# Voice PE sends two mic channels: data = ch0 (XMOS pipeline incl. AGC), data2 = ch1 (no AGC).
# AGC pumps up residual echo of the device's own voice when the user is silent, which trips the
# server VAD; ch1 is the better feed for the model. Recordings keep both (stereo: L=ch0, R=ch1).
MIC_CHANNEL = int(os.environ.get("RTBRIDGE_MIC_CHANNEL", "1"))
# ch1 is ~30 dB quieter than ch0 (speech peak ≈0.03 FS, residual echo ≈0.003); x16 brings speech to
# a healthy level for the model's VAD/ASR while echo stays ~40 dB below speech.
MIC_GAIN = float(os.environ.get("RTBRIDGE_MIC_GAIN", "16" if MIC_CHANNEL == 1 else "1"))


def resample_16k_to_24k(pcm: bytes, state: dict) -> bytes:
    """s16le 16 kHz → 24 kHz by linear interpolation (ratio 2:3), continuous across chunks."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) == 0:
        return b""
    prev = state.get("prev")
    if prev is not None:
        x = np.concatenate([[prev], x])
        start = 0.0
    else:
        start = 0.0
    state["prev"] = x[-1]
    # output positions in input-sample units, step 2/3
    n_out = int(np.floor((len(x) - 1) * 1.5)) + (0 if prev is None else 0)
    pos = start + np.arange(n_out) * (2.0 / 3.0)
    i = np.floor(pos).astype(np.int64)
    frac = (pos - i).astype(np.float32)
    i = np.clip(i, 0, len(x) - 2)
    y = x[i] * (1 - frac) + x[i + 1] * frac
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


class Session:
    """One conversation: from device start to RUN_END."""

    def __init__(self, app: "App", conversation_id: str, wake_word: str | None):
        self.app = app
        self.id = conversation_id or f"s{int(time.time())}"
        self.wake_word = wake_word
        self.t0 = time.monotonic()
        self.mic_chunks = 0
        self.mute_until = 0.0
        self.mic_bytes = 0
        self.ending = False
        self.done = asyncio.Event()
        self.stream: Stream | None = None
        self.rec: wave.Wave_write | None = None
        self.rs_state: dict = {}
        self.oai = None
        self.user_text: list[str] = []
        self.pending_audio: list[bytes] = []
        self._tasks: list[asyncio.Task] = []

    # ---- common ------------------------------------------------------------------------
    async def start(self):
        dev = self.app.dev
        dev.session_begin()
        if self.app.record_dir:
            p = self.app.record_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{self.id}.wav"
            self.rec = wave.open(str(p), "wb"); self.rec.setnchannels(2); self.rec.setsampwidth(2); self.rec.setframerate(MIC_RATE)
            log.info("recording mic to %s", p)
        if self.app.mode == "loopback":
            self._tasks.append(asyncio.create_task(self._loopback()))
        else:
            self._tasks.append(asyncio.create_task(self._openai()))
        self._tasks.append(asyncio.create_task(self._watchdog()))

    async def on_audio(self, data: bytes, data2: bytes | None):
        self.mic_chunks += 1
        self.mic_bytes += len(data)
        if self.rec:
            a = np.frombuffer(data, dtype=np.int16)
            b = np.frombuffer(data2, dtype=np.int16) if data2 and len(data2) == len(data) else np.zeros_like(a)
            self.rec.writeframes(np.column_stack([a, b]).tobytes())
        if MIC_CHANNEL == 1 and data2:
            data = data2
        if time.monotonic() < self.mute_until:
            data = bytes(len(data))   # echo guard: AEC leaks the first ~0.7 s of a new announcement
        if MIC_GAIN != 1.0:
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) * MIC_GAIN
            data = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        if self.mic_chunks == 1:
            log.info("first mic chunk after %.2fs (%d bytes, ch2=%s)", time.monotonic() - self.t0, len(data), data2 is not None)
        if self.app.mode == "loopback":
            self.loop_buf.append(data)
        elif self.oai is not None:
            pcm24 = resample_16k_to_24k(data, self.rs_state)
            if self.oai.ready.is_set():
                if self.pending_audio:
                    for p in self.pending_audio:
                        await self.oai.send_audio(p)
                    self.pending_audio.clear()
                await self.oai.send_audio(pcm24)
            else:
                self.pending_audio.append(pcm24)

    async def end(self, reason: str, from_device: bool = False):
        if self.ending:
            return
        self.ending = True
        log.info("session %s ending: %s (%.1fs, mic %d chunks / %.1fs)", self.id, reason,
                 time.monotonic() - self.t0, self.mic_chunks, self.mic_bytes / (2 * MIC_RATE))
        if self.stream:
            self.stream.close(discard=True)
        self.app.dev.stop_playback()   # the device does not stop an announcement on its own stop
        if self.oai:
            await self.oai.close()
        self.app.dev.session_end()
        if self.rec:
            self.rec.close(); self.rec = None
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()
        self.done.set()

    async def _watchdog(self):
        while not self.ending:
            await asyncio.sleep(1)
            age = time.monotonic() - self.t0
            if age > self.app.max_session:
                await self.end("max session length"); return
            if self.oai is not None and self.oai.ready.is_set():
                idle = time.monotonic() - self.oai.last_activity
                if idle > self.app.idle_timeout:
                    await self.end(f"idle {idle:.0f}s"); return
                if self.oai.closed.is_set():
                    await self.end("openai connection closed"); return

    # ---- stage 1: loopback -----------------------------------------------------------
    async def _loopback(self):
        """Collect ~4 s of mic audio, then play it back through the device while the mic keeps streaming."""
        self.loop_buf: list[bytes] = []
        await asyncio.sleep(4.0)
        pcm = b"".join(self.loop_buf)
        chunks_before = self.mic_chunks
        log.info("loopback: captured %.1fs, playing it back", len(pcm) / (2 * MIC_RATE))
        stream, url = self.app.http.new_stream(MIC_RATE)
        self.stream = stream
        self.app.dev.agent_speaking("loopback echo")
        self.app.dev.play_url(url)
        stream.push(pcm); stream.close()
        await asyncio.sleep(len(pcm) / (2 * MIC_RATE) + 1.5)
        log.info("loopback: mic chunks during playback: %d (full duplex %s)",
                 self.mic_chunks - chunks_before, "OK" if self.mic_chunks - chunks_before > 20 else "FAILED")
        await self.app.dev.wait_playback_done(10)
        await self.end("loopback done")

    # ---- stage 2: OpenAI --------------------------------------------------------------
    async def _openai(self):
        from .openai_rt import RealtimeSession
        dev, http = self.app.dev, self.app.http
        agent_text = {"cur": ""}

        def on_audio(pcm: bytes):
            if self.stream is None or self.stream.closed:
                self.stream, url = http.new_stream(OAI_RATE)
                dev.agent_speaking(agent_text["cur"] or "…")
                dev.play_url(url)
                self.mute_until = time.monotonic() + float(os.environ.get("RTBRIDGE_ECHO_GUARD", "0.8"))
                log.info("agent audio → %s", url)
            self.stream.push(pcm)

        def on_audio_done():
            if self.stream:
                self.stream.close()

        def on_speech_started():
            log.info("user speech started → barge-in")
            if self.stream and not self.stream.closed:
                self.stream.close(discard=True)
            dev.stop_playback()
            dev.user_speaking()

        async def on_tool(name: str, call_id: str, args: dict):
            if name == "end_conversation":
                quote = (args.get("quote") or "").strip()
                # Guard against hallucinated farewells (seen on an empty transcript): require the
                # quoted words to be non-trivial and to have actually been transcribed from the user.
                heard = " ".join(self.user_text[-3:]).lower()
                if len(quote) < 3 or not any(w in heard for w in quote.lower().split() if len(w) > 2):
                    log.warning("end_conversation refused: quote=%r not in recent user text %r", quote, heard)
                    return {"ok": False, "error": "пользователь не прощался — продолжай разговор"}
                log.info("agent asked to end the conversation: %r", quote)
                asyncio.get_running_loop().create_task(self._end_after_playback("agent: end_conversation"))
                return {"ok": True}
            return {"error": f"unknown tool {name}"}

        self.oai = RealtimeSession(
            self.app.openai_key, model=self.app.model, voice=self.app.voice, instructions=self.app.instructions,
            on_audio=on_audio, on_audio_done=on_audio_done, on_speech_started=on_speech_started,
            on_agent_transcript=lambda t: (log.info("AGENT: %s", t), agent_text.__setitem__("cur", "")),
            on_user_transcript=lambda t: (log.info("USER:  %s", t), self.user_text.append(t)),
            on_tool=on_tool)
        try:
            t = time.monotonic()
            await self.oai.connect()
            await asyncio.wait_for(self.oai.ready.wait(), 15)
            log.info("openai session ready in %.2fs", time.monotonic() - t)
        except Exception as e:
            log.error("openai connect failed: %s", e)
            dev.error("openai", str(e))
            await self.end("openai connect failed")
            return
        if self.app.greeting:
            await self.oai.request_response("Поздоровайся одним коротким словом или фразой, например «Слушаю!» или «Да, солнышко?».")

    async def _end_after_playback(self, reason: str):
        if self.stream and not self.stream.closed:
            # wait for the model to finish sending the goodbye
            for _ in range(100):
                await asyncio.sleep(0.1)
                if self.stream.closed:
                    break
        await self.app.dev.wait_playback_done(20)
        await self.end(reason)


class App:
    def __init__(self, a):
        self.mode = a.mode
        self.record_dir = Path(a.record_dir) if a.record_dir else None
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        self.idle_timeout = float(os.environ.get("RTBRIDGE_IDLE_TIMEOUT", "25"))
        self.max_session = float(os.environ.get("RTBRIDGE_MAX_SESSION", "600"))
        self.greeting = os.environ.get("RTBRIDGE_GREETING", "1") == "1"
        self.instructions = os.environ.get("RTBRIDGE_INSTRUCTIONS") or None
        self.openai_key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
        self.voice = os.environ.get("OPENAI_REALTIME_VOICE", "marin")
        if self.mode == "openai" and not self.openai_key:
            raise SystemExit("OPENAI_API_KEY missing (rtbridge/.env)")
        psk = os.environ.get("VOICE_PE_PSK") or psk_from_yaml(os.environ.get("VOICE_PE_YAML", "/opt/home-assistant-voice-sun.yaml"))
        if not psk:
            raise SystemExit("no VOICE_PE_PSK and no key found in VOICE_PE_YAML")
        self.http = AudioHTTP("0.0.0.0", int(os.environ.get("RTBRIDGE_HTTP_PORT", "8766")),
                              public_host=os.environ.get("RTBRIDGE_HTTP_HOST", "192.168.68.79"))
        self._host, self._psk = os.environ.get("VOICE_PE_HOST", "192.168.68.83"), psk
        self.dev: VoicePE | None = None   # APIClient must be created inside the event loop
        self.session: Session | None = None

    async def _on_start(self, conversation_id: str, wake_word: str | None):
        if self.session and not self.session.ending:
            await self.session.end("new start from device", from_device=True)
        self.session = Session(self, conversation_id, wake_word)
        await self.session.start()

    async def _on_audio(self, data: bytes, data2: bytes | None):
        if self.session and not self.session.ending:
            await self.session.on_audio(data, data2)

    async def _on_stop(self, aborted: bool):
        if self.session and not self.session.ending:
            await self.session.end("device stop", from_device=True)

    async def run(self):
        self.dev = VoicePE(self._host, self._psk, on_start=self._on_start, on_audio=self._on_audio, on_stop=self._on_stop)
        await self.http.start()
        await self.dev.start()
        log.info("rtbridge up, mode=%s — say the wake word", self.mode)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            if self.session:
                await self.session.end("shutdown")
            await self.dev.stop()
            await self.http.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["openai", "loopback"], default="openai")
    ap.add_argument("--record-dir", default=os.environ.get("RTBRIDGE_RECORD_DIR") or None,
                    help="save each session's mic audio as wav here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aioesphomeapi").setLevel(logging.INFO)
    try:
        asyncio.run(App(a).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
