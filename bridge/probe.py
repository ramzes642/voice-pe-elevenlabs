#!/usr/bin/env python3
"""Phase-1 probe for the ElevenLabs Agent.

Headless validation (no microphone): confirms auth, prints the agent's audio
formats / language / LLM / tools (from the REST config), then does a TEXT
round-trip over the WebSocket — capturing the agent's Russian reply, the output
audio (saved to out.wav), and any client_tool_call.

Reads ELEVENLABS_API_KEY / ELEVENLABS_AGENT_ID from bridge/.env. Never prints the key.

    python probe.py            # config + text round-trip
    python probe.py "текст"    # custom message
"""
import asyncio
import base64
import json
import os
import sys
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
    # Save full config OUTSIDE the repo (may contain secrets) for inspection.
    Path("/tmp/el_agent_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    conv = cfg.get("conversation_config", {})
    agent = conv.get("agent", {})
    tts = conv.get("tts", {})
    asr = conv.get("asr", {})
    tools = _dig(agent, "prompt", "tools", default=[]) or []
    print("name:           ", cfg.get("name"))
    print("language:       ", agent.get("language"))
    print("LLM:            ", _dig(agent, "prompt", "llm"))
    print("TTS out format: ", tts.get("output_format"), "| voice:", tts.get("voice_id"))
    print("user-in format: ", conv.get("user_input_audio_format") or asr.get("user_input_audio_format"))
    print("tools:          ", [(t.get("type"), t.get("name")) for t in tools] or "none")
    print("(full config -> /tmp/el_agent_config.json)")
    out_fmt = tts.get("output_format") or "pcm_16000"
    return out_fmt


def _wav_params(out_fmt):
    # out_fmt like "pcm_16000" / "pcm_22050" / "pcm_44100" / "ulaw_8000" / "mp3_44100_128"
    if out_fmt.startswith("pcm_"):
        return int(out_fmt.split("_")[1]), 2, "pcm"
    if out_fmt.startswith("ulaw_"):
        return int(out_fmt.split("_")[1]), 1, "ulaw"
    return None, None, out_fmt.split("_")[0]  # e.g. mp3


async def ws_round_trip(text, out_fmt):
    print("\n=== WEBSOCKET ROUND-TRIP ===")
    rate, width, kind = _wav_params(out_fmt)
    audio = bytearray()
    events = {}
    try:
        async with websockets.connect(WS, additional_headers={"xi-api-key": API_KEY},
                                      max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(f"sent (ru): {text!r}\nlistening...\n")
            loop = asyncio.get_event_loop()
            start = loop.time()
            last_audio = None
            MAX_SECONDS = 20.0
            while True:
                # Hard deadline + "quiet after audio" exit. Checked every iteration so
                # periodic pings (which keep recv() busy) can't prevent termination.
                now = loop.time()
                if now - start > MAX_SECONDS:
                    break
                if audio and last_audio and now - last_audio > 2.5:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                evt = json.loads(raw)
                t = evt.get("type", "?")
                events[t] = events.get(t, 0) + 1
                if t == "ping":
                    eid = _dig(evt, "ping_event", "event_id")
                    await ws.send(json.dumps({"type": "pong", "event_id": eid}))
                elif t == "audio":
                    b64 = _dig(evt, "audio_event", "audio_base_64") or evt.get("audio_base_64")
                    if b64:
                        audio += base64.b64decode(b64)
                        last_audio = loop.time()
                elif t == "user_transcript":
                    print("  USER (stt):", _dig(evt, "user_transcription_event", "user_transcript"))
                elif t == "agent_response":
                    print("  AGENT:     ", _dig(evt, "agent_response_event", "agent_response"))
                elif t == "client_tool_call":
                    call = evt.get("client_tool_call", evt)
                    print("  TOOL CALL: ", json.dumps(call, ensure_ascii=False))
                    await ws.send(json.dumps({
                        "type": "client_tool_result",
                        "tool_call_id": call.get("tool_call_id"),
                        "result": "ok (probe stub)",
                        "is_error": False,
                    }))
                elif t in ("interruption", "vad_score", "internal_tentative_agent_response"):
                    pass
                else:
                    print(f"  [{t}] {json.dumps(evt, ensure_ascii=False)[:160]}")
    except Exception as e:
        print("WS error:", type(e).__name__, e)

    print("\n=== RESULT ===")
    print("event counts:", events)
    print(f"audio bytes: {len(audio)}  (format {out_fmt})")
    if audio and kind == "pcm":
        with wave.open(str(HERE / "out.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(width); w.setframerate(rate)
            w.writeframes(bytes(audio))
        secs = len(audio) / (width * rate)
        print(f"saved out.wav ({secs:.1f}s, {rate} Hz mono) — play to verify Russian voice")
    elif audio:
        ext = "ulaw" if kind == "ulaw" else (kind or "bin")
        (HERE / f"out.{ext}").write_bytes(bytes(audio))
        print(f"saved out.{ext} (format {out_fmt}; convert to verify)")


async def main():
    out_fmt = fetch_config()
    text = sys.argv[1] if len(sys.argv) > 1 else "Привет! Ответь одним коротким предложением по-русски: как у тебя дела?"
    await ws_round_trip(text, out_fmt)


if __name__ == "__main__":
    asyncio.run(main())
