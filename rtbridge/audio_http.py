"""Tiny HTTP server the Voice PE pulls response audio from.

Each agent response becomes one URL: http://<pi>:<port>/stream/<id>.wav — a chunked WAV
(s16le mono @ rate) that we feed from a queue as audio arrives from the model. Closing the
queue (push None) ends the stream; the device's media player then returns to idle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
import uuid

from aiohttp import web

log = logging.getLogger("rtbridge.http")


def wav_header(rate: int, channels: int = 1, bits: int = 16, data_len: int = 0xFFFFFFFF - 44) -> bytes:
    byte_rate = rate * channels * bits // 8
    return (b"RIFF" + struct.pack("<I", data_len + 36) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * bits // 8, bits)
            + b"data" + struct.pack("<I", data_len))


class Stream:
    def __init__(self, rate: int):
        self.id = uuid.uuid4().hex[:12]
        self.rate = rate
        self.q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self.bytes = 0
        self.discard = False
        self.fetched = asyncio.Event()

    def push(self, pcm: bytes):
        if not self.closed:
            self.bytes += len(pcm)
            self.q.put_nowait(pcm)

    def close(self, discard: bool = False):
        """End the stream. discard=True drops audio not yet sent (barge-in / session end)."""
        if not self.closed:
            self.closed = True
            if discard:
                self.discard = True
                while not self.q.empty():
                    try: self.q.get_nowait()
                    except asyncio.QueueEmpty: break
            self.q.put_nowait(None)


class AudioHTTP:
    def __init__(self, host: str, port: int, public_host: str | None = None):
        self.host, self.port = host, port
        self.public_host = public_host or host
        self.streams: dict[str, Stream] = {}
        self.app = web.Application()
        self.app.router.add_get("/stream/{sid}.wav", self._serve)
        self.app.router.add_get("/health", lambda r: web.Response(text="ok"))
        self._runner: web.AppRunner | None = None

    async def start(self):
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        log.info("audio http on http://%s:%d/stream/<id>.wav", self.public_host, self.port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def new_stream(self, rate: int) -> tuple[Stream, str]:
        s = Stream(rate)
        self.streams[s.id] = s
        # drop old finished streams
        for sid in [k for k, v in self.streams.items() if v.closed and v.fetched.is_set() and k != s.id][:-4]:
            self.streams.pop(sid, None)
        return s, f"http://{self.public_host}:{self.port}/stream/{s.id}.wav"

    async def _serve(self, request: web.Request):
        s = self.streams.get(request.match_info["sid"])
        if s is None:
            raise web.HTTPNotFound()
        s.fetched.set()
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        await resp.write(wav_header(s.rate))
        sent = 0
        # Pace delivery to real time (+ LEAD seconds ahead) so the device never holds more
        # than ~LEAD s of future audio: closing the stream then cuts playback almost at once,
        # without a media_player STOP (stopping a just-started announcement can crash the
        # device's decoder task — double free in pthread TLS cleanup, seen 2026-10-03).
        lead = float(os.environ.get("RTBRIDGE_STREAM_LEAD", "0.6"))
        byte_rate = 2 * s.rate
        t0 = time.monotonic()
        try:
            while True:
                chunk = await s.q.get()
                if chunk is None:
                    break
                ahead = (sent + len(chunk)) / byte_rate - (time.monotonic() - t0)
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                if s.discard:
                    break
                await resp.write(chunk)
                sent += len(chunk)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, web.HTTPException, OSError, Exception) as e:
            # the device closes the socket when playback is STOPped — not an error for us
            log.debug("stream %s: client went away after %d bytes (%s)", s.id, sent, type(e).__name__)
            return resp
        finally:
            log.info("stream %s done: %d bytes (%.1fs)", s.id, sent, sent / (2 * s.rate))
        await resp.write_eof()
        return resp
