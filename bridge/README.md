# el_bridge

The bridge between the Voice PE's audio stream and the ElevenLabs Agent WebSocket.

Two lives:
1. **Phase 1 — standalone probe** (`probe.py`, done): runs on a laptop, no hardware.
   Fetches the agent config, round-trips text and TTS-generated Russian audio through the
   ElevenLabs Agent WebSocket, exercises a client tool, measures latency. Nailed down the
   audio format (`pcm_16000`), the tool protocol, and Russian. Run: `python probe.py`,
   `python probe.py --text "..."`, `python probe.py --tool`.
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
Phase 1 probe complete; the HA integration (Phase 2) is not started. See `../docs/checklist.md`.
