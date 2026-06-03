#!/usr/bin/env python3
"""Phase-1 probe for the ElevenLabs Agent.

Headless validation (no microphone):
  1. Prints the agent config (audio formats / language / LLM / tools) via REST.
  2. AUDIO round-trip (default): synthesizes a Russian phrase via EL TTS (pcm_16000),
     streams it to the agent as `user_audio_chunk` (real-time paced + trailing silence),
     then captures the Scribe STT transcript, any client_tool_call, the agent's audio
     reply (saved to out.wav), and measures response latency.
  3. TEXT round-trip with `--text "..."`.

Reads ELEVENLABS_API_KEY / ELEVENLABS_AGENT_ID from bridge/.env. Never prints the key.
"""
import asyncio
import base64
import json
import os
import sys
import time
import wave
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
API_KEY = os.environ["ELEVENLABS_API_KEY"]
AGENT_ID = os.environ["ELEVENLABS_AGENT_ID"]
REST = "https://api.elevenlabs.io"
WS = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={AGENT_ID}"
RATE = 16000  # pcm_16000


def _dig(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def fetch_config():
    print("=== AGENT CONFIG (REST) ===")
    r = httpx.get(f"{REST}/v1/convai/agents/{AGENT_ID}",
                  headers={"xi-api-key": API_KEY}, timeout=30)
    r.raise_for_status()
    cfg = r.json()
    Path("/tmp/el_agent_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    conv = cfg.get("conversation_config", {})
    agent = conv.get("agent", {})
    tts = conv.get("tts", {})
    tools = _dig(agent, "prompt", "tools", default=[]) or []
    voice = tts.get("voice_id")
    print("name:           ", cfg.get("name"))
    print("language:       ", agent.get("language"))
    print("LLM:            ", _dig(agent, "prompt", "llm"))
    print("TTS voice:      ", voice)
    print("tools:          ", [(t.get("type"), t.get("name")) for t in tools] or "none")
    return voice or "cjVigY5qzO86Huf0OWal"


def tts_pcm16k(text, voice):
    """Synthesize Russian speech as raw pcm_16000 mono — used as fake mic input."""
    r = httpx.post(f"{REST}/v1/text-to-speech/{voice}",
                   params={"output_format": "pcm_16000"},
                   headers={"xi-api-key": API_KEY, "content-type": "application/json"},
                   json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
    r.raise_for_status()
    return r.content


async def _reader(ws, ctx):
    loop = asyncio.get_event_loop()
    while True:
        now = loop.time()
        if now - ctx["start"] > 30:
            break
        if ctx.get("input_end") and ctx["audio"] and ctx["last_audio"] and now - ctx["last_audio"] > 2.5:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            break
        evt = json.loads(raw)
        t = evt.get("type", "?")
        ctx["events"][t] = ctx["events"].get(t, 0) + 1
        if t == "ping":
            await ws.send(json.dumps({"type": "pong", "event_id": _dig(evt, "ping_event", "event_id")}))
        elif t == "audio":
            b64 = _dig(evt, "audio_event", "audio_base_64") or evt.get("audio_base_64")
            if b64:
                ctx["audio"] += base64.b64decode(b64)
                t_now = loop.time()
                ctx["last_audio"] = t_now
                if ctx.get("input_end") and ctx.get("first_audio") is None:
                    ctx["first_audio"] = t_now
        elif t == "user_transcript":
            print("  USER STT:  ", _dig(evt, "user_transcription_event", "user_transcript"))
        elif t == "agent_response":
            print("  AGENT:     ", _dig(evt, "agent_response_event", "agent_response"))
        elif t == "client_tool_call":
            call = evt.get("client_tool_call", evt)
            print("  TOOL CALL: ", json.dumps(call, ensure_ascii=False))
            await ws.send(json.dumps({
                "type": "client_tool_result",
                "tool_call_id": call.get("tool_call_id"),
                "result": "ok (probe stub: AC turned on)",
                "is_error": False,
            }))
        elif t in ("interruption", "vad_score", "internal_tentative_agent_response",
                   "agent_response_correction", "conversation_initiation_metadata"):
            pass
        else:
            print(f"  [{t}] {json.dumps(evt, ensure_ascii=False)[:140]}")


def _new_ctx():
    return {"events": {}, "audio": bytearray(), "last_audio": None,
            "first_audio": None, "input_end": None, "start": asyncio.get_event_loop().time()}


async def audio_trip(phrase, voice):
    print(f"\n=== AUDIO ROUND-TRIP ===\nphrase (TTS→mic): {phrase!r}")
    pcm = tts_pcm16k(phrase, voice)
    print(f"synthesized {len(pcm)/2/RATE:.1f}s of pcm_16000 input")
    async with websockets.connect(WS, additional_headers={"xi-api-key": API_KEY},
                                  max_size=None, open_timeout=20) as ws:
        ctx = _new_ctx()
        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
        reader = asyncio.create_task(_reader(ws, ctx))
        # Let the session settle (greeting starts) so the first words aren't dropped,
        # and prepend ~0.6s lead-in silence so the opening word isn't clipped by VAD.
        await asyncio.sleep(1.0)
        pcm = b"\x00" * (int(RATE * 0.6) * 2) + pcm
        # stream input as 0.25s chunks at real-time pace, then ~1.5s silence to end the turn
        chunk = int(RATE * 0.25) * 2  # bytes per 250 ms
        for i in range(0, len(pcm), chunk):
            await ws.send(json.dumps({"user_audio_chunk": base64.b64encode(pcm[i:i + chunk]).decode()}))
            await asyncio.sleep(0.25)
        sil = base64.b64encode(b"\x00" * chunk).decode()
        for _ in range(6):
            await ws.send(json.dumps({"user_audio_chunk": sil}))
            await asyncio.sleep(0.25)
        ctx["input_end"] = asyncio.get_event_loop().time()
        print("  (input sent, waiting for response...)")
        await reader
    _report(ctx)


async def text_trip(text):
    print(f"\n=== TEXT ROUND-TRIP ===\nsent: {text!r}")
    async with websockets.connect(WS, additional_headers={"xi-api-key": API_KEY},
                                  max_size=None, open_timeout=20) as ws:
        ctx = _new_ctx()
        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
        await ws.send(json.dumps({"type": "user_message", "text": text}))
        ctx["input_end"] = asyncio.get_event_loop().time()
        await _reader(ws, ctx)
    _report(ctx)


async def _drain(ws, ctx, turn_timeout=12.0):
    """Read events for one turn: until ~2s quiet after audio, or turn_timeout."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    last = None
    while True:
        now = loop.time()
        if now - t0 > turn_timeout:
            break
        if last and now - last > 2.0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            break
        evt = json.loads(raw)
        t = evt.get("type", "?")
        ctx["events"][t] = ctx["events"].get(t, 0) + 1
        if t == "ping":
            await ws.send(json.dumps({"type": "pong", "event_id": _dig(evt, "ping_event", "event_id")}))
        elif t == "audio":
            b64 = _dig(evt, "audio_event", "audio_base_64")
            if b64:
                ctx["audio"] += base64.b64decode(b64)
                last = loop.time()
        elif t == "user_transcript":
            print("  USER STT:  ", _dig(evt, "user_transcription_event", "user_transcript"))
        elif t == "agent_response":
            print("  AGENT:     ", _dig(evt, "agent_response_event", "agent_response"))
        elif t == "client_tool_call":
            call = evt.get("client_tool_call", evt)
            print("  *** TOOL CALL:", json.dumps(call, ensure_ascii=False))
            ctx["tool_calls"].append(call)
            await ws.send(json.dumps({
                "type": "client_tool_result",
                "tool_call_id": call.get("tool_call_id"),
                "result": "Кондиционер включён (probe stub)",
                "is_error": False,
            }))
            last = loop.time()


async def convo_trip(messages):
    print("\n=== MULTI-TURN (text) ===")
    async with websockets.connect(WS, additional_headers={"xi-api-key": API_KEY},
                                  max_size=None, open_timeout=20) as ws:
        ctx = _new_ctx(); ctx["tool_calls"] = []
        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
        await _drain(ws, ctx, turn_timeout=8)  # consume the greeting
        for m in messages:
            print(f"\n>>> USER: {m!r}")
            await ws.send(json.dumps({"type": "user_message", "text": m}))
            await _drain(ws, ctx)
    print("\n=== RESULT ===")
    print("tool calls:", json.dumps(ctx["tool_calls"], ensure_ascii=False))
    print("event counts:", ctx["events"])


def _report(ctx):
    print("\n=== RESULT ===")
    print("event counts:", ctx["events"])
    print(f"agent audio: {len(ctx['audio'])} bytes (pcm_16000)")
    if ctx["first_audio"] and ctx["input_end"]:
        print(f"latency (end-of-input → first audio): {ctx['first_audio'] - ctx['input_end']:.2f}s")
    if ctx["audio"]:
        with wave.open(str(HERE / "out.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(bytes(ctx["audio"]))
        print(f"saved out.wav ({len(ctx['audio'])/2/RATE:.1f}s) — play to verify Russian voice")


async def main():
    voice = fetch_config()
    if len(sys.argv) > 1 and sys.argv[1] == "--tool":
        await convo_trip(["Включи кондиционер", "Да, включай, спасибо!"])
    elif len(sys.argv) > 2 and sys.argv[1] == "--text":
        await text_trip(sys.argv[2])
    else:
        phrase = sys.argv[1] if len(sys.argv) > 1 else "Включи кондиционер, пожалуйста."
        await audio_trip(phrase, voice)


if __name__ == "__main__":
    asyncio.run(main())
