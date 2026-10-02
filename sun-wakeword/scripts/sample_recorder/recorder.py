"""Wyoming "STT" service that just SAVES received audio as 16 kHz mono wav files.

Used to collect real wake-word samples straight from the HA Voice PE: a dedicated
Assist pipeline points its speech-to-text at this service, so every utterance the
speaker streams gets written to disk (instead of being transcribed). The saved wav
is already 16 kHz mono 16-bit PCM — the exact format the training pipeline expects.

Listens on tcp://0.0.0.0:10500. Saves to $OUT_DIR (default /data), filename
<PREFIX>_<unix_ms>.wav. Returns an empty transcript so the pipeline completes cleanly.
"""
import argparse
import asyncio
import logging
import time
import wave
from functools import partial
from pathlib import Path

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, AsrModel, AsrProgram, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

_LOGGER = logging.getLogger("recorder")

INFO = Info(
    asr=[
        AsrProgram(
            name="sample-recorder",
            description="Saves incoming audio as wav (sample collection)",
            installed=True,
            version="1.0",
            attribution=Attribution(name="local", url="https://localhost"),
            models=[
                AsrModel(
                    name="recorder",
                    description="recorder",
                    installed=True,
                    version="1.0",
                    attribution=Attribution(name="local", url="https://localhost"),
                    languages=["ru", "en"],
                )
            ],
        )
    ]
)


class RecorderHandler(AsyncEventHandler):
    def __init__(self, *args, out_dir: Path, prefix: str, min_ms: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.out_dir = out_dir
        self.prefix = prefix
        self.min_bytes = int(min_ms / 1000 * 16000 * 2)  # assume 16k/16-bit for threshold
        self.buf = bytearray()
        self.rate, self.width, self.channels = 16000, 2, 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(INFO.event())
            return True
        if Transcribe.is_type(event.type):
            self.buf = bytearray()
            return True
        if AudioStart.is_type(event.type):
            s = AudioStart.from_event(event)
            self.rate, self.width, self.channels = s.rate, s.width, s.channels
            self.buf = bytearray()
            _LOGGER.info("stream start: %d Hz, %d ch, %d-bit", s.rate, s.channels, s.width * 8)
            return True
        if AudioChunk.is_type(event.type):
            self.buf.extend(AudioChunk.from_event(event).audio)
            return True
        if AudioStop.is_type(event.type):
            self._save()
            await self.write_event(Transcript(text="").event())
            return True
        return True

    def _save(self) -> None:
        if len(self.buf) < self.min_bytes:
            _LOGGER.warning("discarded short clip: %d bytes (< %d)", len(self.buf), self.min_bytes)
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self.prefix}_{int(time.time() * 1000)}.wav"
        path = self.out_dir / name
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.width)
            wf.setframerate(self.rate)
            wf.writeframes(bytes(self.buf))
        dur = len(self.buf) / (self.rate * self.width * self.channels)
        _LOGGER.info("saved %s (%.2fs, %d Hz, %d ch)", path, dur, self.rate, self.channels)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--uri", default="tcp://0.0.0.0:10500")
    p.add_argument("--out-dir", default="/data")
    p.add_argument("--prefix", default="sun")
    p.add_argument("--min-ms", type=int, default=300)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)

    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("recorder listening on %s -> %s", args.uri, args.out_dir)
    await server.run(
        partial(RecorderHandler, out_dir=Path(args.out_dir), prefix=args.prefix, min_ms=args.min_ms)
    )


if __name__ == "__main__":
    asyncio.run(main())
