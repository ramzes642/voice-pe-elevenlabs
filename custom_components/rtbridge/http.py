"""HTTP view the Voice PE pulls response audio from (chunked WAV, paced to real time)."""
from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STREAM_PATH

log = logging.getLogger(__name__)
STREAMS_KEY = f"{DOMAIN}_streams"


def wav_header(rate: int, channels: int = 1, bits: int = 16, data_len: int = 0xFFFFFFFF - 44) -> bytes:
    byte_rate = rate * channels * bits // 8
    return (b"RIFF" + struct.pack("<I", data_len + 36) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * bits // 8, bits)
            + b"data" + struct.pack("<I", data_len))


class Stream:
    def __init__(self, rate: int, lead: float = 0.6):
        self.id = uuid.uuid4().hex[:12]
        self.rate, self.lead = rate, lead
        self.q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self.discard = False

    def push(self, pcm: bytes):
        if not self.closed:
            self.q.put_nowait(pcm)

    def close(self, discard: bool = False):
        if not self.closed:
            self.closed = True
            if discard:
                self.discard = True
                while not self.q.empty():
                    try:
                        self.q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            self.q.put_nowait(None)


def streams(hass: HomeAssistant) -> dict[str, Stream]:
    return hass.data.setdefault(STREAMS_KEY, {})


def new_stream(hass: HomeAssistant, rate: int, lead: float = 0.6) -> Stream:
    reg = streams(hass)
    s = Stream(rate, lead)
    reg[s.id] = s
    for sid in [k for k, v in reg.items() if v.closed and k != s.id][:-4]:
        reg.pop(sid, None)
    return s


class StreamView(HomeAssistantView):
    url = STREAM_PATH
    name = "api:rtbridge:stream"
    requires_auth = False   # the device fetches it; ids are random and single-use

    async def get(self, request: web.Request, sid: str):
        s = streams(request.app["hass"]).get(sid)
        if s is None:
            raise web.HTTPNotFound()
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        await resp.write(wav_header(s.rate))
        sent, byte_rate, t0 = 0, 2 * s.rate, time.monotonic()
        try:
            while True:
                chunk = await s.q.get()
                if chunk is None:
                    break
                ahead = (sent + len(chunk)) / byte_rate - (time.monotonic() - t0)
                if ahead > s.lead:
                    await asyncio.sleep(ahead - s.lead)
                if s.discard:
                    break
                await resp.write(chunk)
                sent += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # device closes the socket on STOP — fine
            log.debug("stream %s: client gone after %d bytes (%s)", s.id, sent, type(e).__name__)
            return resp
        log.debug("stream %s done: %d bytes (%.1fs)", s.id, sent, sent / byte_rate)
        await resp.write_eof()
        return resp
