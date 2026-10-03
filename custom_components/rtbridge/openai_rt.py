"""OpenAI Realtime WebSocket session on aiohttp (HA's client session).

  wss://api.openai.com/v1/realtime?model=…   Authorization: Bearer <key>
  session.update {session:{type:"realtime", instructions, output_modalities:["audio"],
                  audio:{input:{format:{type:"audio/pcm",rate:24000}, turn_detection, noise_reduction, transcription},
                         output:{format, voice}}, tools, tool_choice}}
  → input_audio_buffer.append {audio: b64 pcm16@24k}
  ← response.output_audio.delta / .done, response.done, input_audio_buffer.speech_started,
    response.function_call_arguments.done → conversation.item.create(function_call_output)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)
RATE = 24000


class RealtimeSession:
    def __init__(self, http: aiohttp.ClientSession, api_key: str, *, model: str, voice: str, instructions: str,
                 tools: list[dict], language: str = "ru", eagerness: str = "auto",
                 on_audio: Callable[[bytes], None], on_audio_done: Callable[[], None],
                 on_speech_started: Callable[[], None],
                 on_agent_transcript: Callable[[str], None] | None = None,
                 on_user_transcript: Callable[[str], None] | None = None,
                 on_tool: Callable[[str, str, dict], Awaitable[dict | str]] | None = None):
        self.http = http
        self.url = f"wss://api.openai.com/v1/realtime?model={model}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.voice, self.instructions, self.tools, self.language, self.eagerness = voice, instructions, tools, language, eagerness
        self.on_audio, self.on_audio_done, self.on_speech_started = on_audio, on_audio_done, on_speech_started
        self.on_agent_transcript, self.on_user_transcript, self.on_tool = on_agent_transcript, on_user_transcript, on_tool
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.last_activity = time.monotonic()
        self.speech_stopped_at: float | None = None   # for the stuck-turn watchdog
        self.response_active = False
        self._task: asyncio.Task | None = None

    async def connect(self):
        self.ws = await self.http.ws_connect(self.url, headers=self.headers, max_msg_size=0, heartbeat=20, timeout=15)
        await self.send({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE},
                          "turn_detection": {"type": "semantic_vad", "eagerness": self.eagerness,
                                             "create_response": True, "interrupt_response": True},
                          "noise_reduction": {"type": "far_field"},
                          "transcription": {"model": "gpt-4o-mini-transcribe", "language": self.language}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.voice},
            },
            "tools": self.tools, "tool_choice": "auto",
        }})
        self._task = asyncio.create_task(self._recv(), name="rtbridge-openai-recv")

    async def close(self):
        if self._task:
            self._task.cancel()
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.closed.set()

    async def send(self, event: dict):
        if self.ws and not self.ws.closed:
            await self.ws.send_str(json.dumps(event))

    async def send_audio(self, pcm24k: bytes):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm24k).decode()})

    async def request_response(self, instructions: str | None = None):
        ev: dict[str, Any] = {"type": "response.create"}
        if instructions:
            ev["response"] = {"instructions": instructions}
        await self.send(ev)

    async def inject_text(self, text: str, role: str = "user", respond: bool = True):
        await self.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}})
        if respond:
            await self.request_response()

    def turn_stuck(self, after: float) -> bool:
        """semantic_vad sometimes never ends a turn (seen: 10 s stall). True if speech stopped
        `after` seconds ago and no response has started since."""
        return (self.speech_stopped_at is not None and not self.response_active
                and time.monotonic() - self.speech_stopped_at > after)

    async def force_turn(self):
        log.info("turn detection stalled — forcing commit + response")
        self.speech_stopped_at = None
        await self.send({"type": "input_audio_buffer.commit"})
        await self.request_response()

    async def _recv(self):
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                ev = json.loads(msg.data)
                t = ev.get("type", "")
                if t == "response.output_audio.delta":
                    self.last_activity = time.monotonic()
                    self.on_audio(base64.b64decode(ev["delta"]))
                elif t == "response.created":
                    self.response_active = True
                    self.speech_stopped_at = None
                elif t in ("response.output_audio.done", "response.done"):
                    self.on_audio_done()
                    if t == "response.done":
                        self.response_active = False
                        status = ev.get("response", {}).get("status")
                        if status not in ("completed", "cancelled"):
                            log.warning("response.done status=%s %s", status,
                                        json.dumps(ev.get("response", {}).get("status_details"), ensure_ascii=False))
                elif t == "input_audio_buffer.speech_started":
                    self.last_activity = time.monotonic()
                    self.speech_stopped_at = None
                    self.on_speech_started()
                elif t == "input_audio_buffer.speech_stopped":
                    self.speech_stopped_at = time.monotonic()
                elif t == "response.output_audio_transcript.done":
                    if self.on_agent_transcript:
                        self.on_agent_transcript(ev.get("transcript", ""))
                elif t == "conversation.item.input_audio_transcription.completed":
                    if self.on_user_transcript:
                        self.on_user_transcript(ev.get("transcript", ""))
                elif t == "response.function_call_arguments.done":
                    name, call_id = ev.get("name"), ev.get("call_id")
                    try:
                        args = json.loads(ev.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    log.info("tool call %s(%s)", name, args)
                    if self.on_tool:
                        result = await self.on_tool(name, call_id, args)
                        await self.send({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": call_id,
                            "output": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}})
                        if not (isinstance(result, dict) and result.get("_no_response")):
                            await self.request_response()
                elif t == "session.updated":
                    self.ready.set()
                elif t == "error":
                    log.error("openai error: %s", json.dumps(ev.get("error"), ensure_ascii=False))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("openai ws loop ended: %s", e)
        finally:
            self.closed.set()
