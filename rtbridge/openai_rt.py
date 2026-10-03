"""OpenAI Realtime WebSocket session, headless (audio in/out via callbacks).

Protocol (developers.openai.com/api/docs/guides/realtime, 2026-10):
  wss://api.openai.com/v1/realtime?model=…  Authorization: Bearer <key>
  session.update {session:{type:"realtime", instructions, output_modalities:["audio"],
                  audio:{input:{format:{type:"audio/pcm",rate:24000}, turn_detection:{type:"server_vad",…},
                               transcription:{model}}, output:{format:{…}, voice}}, tools:[…]}}
  → input_audio_buffer.append {audio: b64 pcm16@24k}
  ← response.output_audio.delta {delta: b64}, response.output_audio.done, response.done
  ← input_audio_buffer.speech_started (barge-in: server already cancelled the response)
  ← response.function_call_arguments.done {name, call_id, arguments}
  → conversation.item.create {item:{type:"function_call_output", call_id, output}}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("rtbridge.openai")
RATE = 24000

DEFAULT_INSTRUCTIONS = (
    "Ты — голосовой ассистент умной колонки «Солнце». Говори только по-русски, коротко и живо, "
    "одно-два предложения, без списков и без markdown. Тебя могут перебивать — это нормально. "
    "Если пользователь прощается, говорит «хватит», «спасибо, всё», «пока» или просит замолчать — "
    "коротко попрощайся и в том же ответе вызови инструмент end_conversation, передав его точные слова. "
    "Без явного прощания инструмент не вызывай. "
    "Если реплика неразборчива или похожа на шум или обрывок, не переспрашивай длинно — скажи только «М?»."
)

TOOLS = [
    {
        "type": "function",
        "name": "end_conversation",
        "description": "Завершить голосовую сессию и перевести колонку в режим ожидания ключевого слова. "
                       "Вызывай ТОЛЬКО если пользователь ЯВНО попрощался или попросил закончить "
                       "(«пока», «спасибо, всё», «хватит», «отбой», «замолчи»). Никогда не вызывай, если реплика "
                       "пустая, неразборчивая или если прощание лишь подразумевается. Сначала коротко попрощайся.",
        "parameters": {"type": "object", "properties": {
            "quote": {"type": "string", "description": "дословные слова пользователя, которыми он попрощался"}},
            "required": ["quote"]},
    },
]


class RealtimeSession:
    def __init__(self, api_key: str, *, model: str, voice: str, instructions: str | None = None,
                 on_audio: Callable[[bytes], None], on_audio_done: Callable[[], None],
                 on_speech_started: Callable[[], None], on_speech_stopped: Callable[[], None] | None = None,
                 on_agent_transcript: Callable[[str], None] | None = None,
                 on_user_transcript: Callable[[str], None] | None = None,
                 on_tool: Callable[[str, str, dict], Awaitable[dict | str]] | None = None,
                 on_response_started: Callable[[], None] | None = None,
                 vad: dict | None = None):
        self.url = f"wss://api.openai.com/v1/realtime?model={model}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.voice = voice
        self.instructions = instructions or DEFAULT_INSTRUCTIONS
        self.on_audio, self.on_audio_done = on_audio, on_audio_done
        self.on_speech_started, self.on_speech_stopped = on_speech_started, on_speech_stopped
        self.on_agent_transcript, self.on_user_transcript = on_agent_transcript, on_user_transcript
        self.on_tool, self.on_response_started = on_tool, on_response_started
        if vad:
            self.vad = vad
        elif os.environ.get("RTBRIDGE_VAD", "semantic") == "semantic":
            # semantic_vad waits for the thought to finish instead of cutting at every 0.7 s pause
            self.vad = {"type": "semantic_vad", "eagerness": os.environ.get("RTBRIDGE_VAD_EAGERNESS", "auto"),
                        "create_response": True, "interrupt_response": True}
        else:
            self.vad = {"type": "server_vad",
                        "threshold": float(os.environ.get("RTBRIDGE_VAD_THRESHOLD", "0.6")),
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": int(os.environ.get("RTBRIDGE_VAD_SILENCE_MS", "700")),
                        "create_response": True, "interrupt_response": True}
        self.ws = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.last_activity = time.monotonic()
        self._task: asyncio.Task | None = None
        self._active_response: str | None = None

    async def connect(self):
        self.ws = await websockets.connect(self.url, additional_headers=self.headers, max_size=None,
                                           open_timeout=15)
        await self.send({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE}, "turn_detection": self.vad,
                          "noise_reduction": {"type": "far_field"},
                          "transcription": {"model": "gpt-4o-mini-transcribe", "language": "ru"}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.voice},
            },
            "tools": TOOLS, "tool_choice": "auto",
        }})
        self._task = asyncio.create_task(self._recv(), name="openai-recv")

    async def close(self):
        if self._task:
            self._task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.closed.set()

    async def send(self, event: dict):
        await self.ws.send(json.dumps(event))

    async def send_audio(self, pcm24k: bytes):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm24k).decode()})

    async def request_response(self, instructions: str | None = None):
        ev: dict[str, Any] = {"type": "response.create"}
        if instructions:
            ev["response"] = {"instructions": instructions}
        await self.send(ev)

    async def inject_text(self, text: str, role: str = "user", respond: bool = True):
        """Sideband: push a text item into the conversation (e.g. a system notice) and optionally respond."""
        content_type = "input_text"
        await self.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": role, "content": [{"type": content_type, "text": text}]}})
        if respond:
            await self.request_response()

    async def cancel_response(self):
        if self._active_response:
            await self.send({"type": "response.cancel"})

    async def _recv(self):
        try:
            async for raw in self.ws:
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t == "response.output_audio.delta":
                    self.last_activity = time.monotonic()
                    self.on_audio(base64.b64decode(ev["delta"]))
                elif t == "response.output_audio.done":
                    self.on_audio_done()
                elif t == "response.created":
                    self._active_response = ev.get("response", {}).get("id")
                    if self.on_response_started:
                        self.on_response_started()
                elif t == "response.done":
                    self._active_response = None
                    self.on_audio_done()
                    status = ev.get("response", {}).get("status")
                    if status not in ("completed", "cancelled"):
                        log.warning("response.done status=%s details=%s", status,
                                    json.dumps(ev.get("response", {}).get("status_details"), ensure_ascii=False))
                elif t == "input_audio_buffer.speech_started":
                    self.last_activity = time.monotonic()
                    self.on_speech_started()
                elif t == "input_audio_buffer.speech_stopped":
                    if self.on_speech_stopped:
                        self.on_speech_stopped()
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
                elif t == "session.updated":
                    self.ready.set()
                    log.info("session ready")
                elif t == "error":
                    log.error("openai error: %s", json.dumps(ev.get("error"), ensure_ascii=False))
                elif t in ("session.created", "rate_limits.updated", "response.output_item.added",
                           "response.output_item.done", "response.content_part.added",
                           "response.content_part.done", "conversation.item.created",
                           "conversation.item.added", "conversation.item.done",
                           "input_audio_buffer.committed", "input_audio_buffer.cleared",
                           "response.output_audio_transcript.delta",
                           "conversation.item.input_audio_transcription.delta",
                           "response.function_call_arguments.delta"):
                    pass
                else:
                    log.debug("event %s", t)
        except websockets.ConnectionClosed as e:
            log.warning("openai ws closed: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            self.closed.set()
