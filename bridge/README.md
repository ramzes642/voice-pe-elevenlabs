# el_bridge

The bridge between the Voice PE's audio stream and the ElevenLabs Agent WebSocket.

Two lives:
1. **Phase 1 — standalone prototype** (`prototype.py`, not written yet): runs on a
   laptop, no hardware. Feeds a wav / local mic to the ElevenLabs Agent, plays the
   response, exercises a dummy client tool. Used to nail down audio format, the tool
   protocol, and Russian before any HA/firmware work.
2. **Phase 2+ — Home Assistant custom integration** (`custom_components/el_bridge/`):
   reacts to the Voice PE `micro_wake_word` event, relays the device's UDP audio ⇄ the
   ElevenLabs WebSocket, converts/resamples audio, and executes `client_tool_call`s
   (HA service calls, local wolt MCP, hermes via MCP).

## Config (env / HA secrets — never commit)
- `ELEVENLABS_API_KEY` — ElevenLabs API key
- `ELEVENLABS_AGENT_ID` — the configured agent
- (later) wolt MCP launch command / endpoint, HA URL+token

Copy `.env.example` → `.env` (gitignored) for local runs.

## Status
Empty — see `../docs/checklist.md` Phase 1.
