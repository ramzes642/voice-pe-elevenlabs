# Checklist

Keep this updated as work progresses. `[ ]` todo, `[~]` in progress, `[x]` done.

## Phase 0 — Scaffolding
- [x] Create umbrella repo (docs, structure)
- [x] Write README / AGENTS.md / architecture / hardware docs
- [x] `gh auth login` (user, interactive)
- [x] Fork `esphome/home-assistant-voice-pe` → add as submodule `external/`
- [x] Fork `esphome/voice-kit-xmos-firmware` → add as submodule `external/`
- [x] Create **public** GitHub repo + push → https://github.com/ramzes642/voice-pe-elevenlabs

## Phase 1 — Bridge prototype (laptop, NO hardware)  ← in progress
De-risk ElevenLabs before touching firmware.
- [x] Create an ElevenLabs Agent ("Nyan-cat", LLM gemini-2.5-flash) — ⚠️ language=`en`, change to `ru`
- [x] Get `agent_id`; `ELEVENLABS_API_KEY` in `bridge/.env` (gitignored, verified untracked)
- [x] `bridge/probe.py`: WS round-trip (text-in) + REST config fetch + audio capture
- [x] Confirm **audio format**: **pcm_16000 both input & output** (from `conversation_initiation_metadata`)
- [~] Confirm **Russian**: LLM replies in Russian ✓ ("У меня всё хорошо…"); still TODO: set agent
      `language=ru`, then verify Scribe **STT from audio** + TTS voice quality (play `out.wav`)
- [ ] Confirm **client_tool_call / client_tool_result** schema — needs a tool added to the agent (none yet)
- [ ] Feed **real audio** (wav/mic) to validate STT, not just text
- [ ] Measure round-trip latency

Protocol confirmed (see also docs/architecture.md): WS `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=…`,
auth via `xi-api-key` header; events `conversation_initiation_metadata` (carries audio formats + conversation_id),
`agent_response`, `agent_response_correction`, `audio` (`audio_event.audio_base_64`), `ping`→`pong`,
`client_tool_call`→`client_tool_result`; send `user_message`(text) / `user_audio_chunk`(base64 pcm16k).

## Phase 2 — Bridge as a Home Assistant integration
- [ ] Port the prototype into a HA custom integration `el_bridge`
- [ ] Subscribe to the Voice PE `micro_wake_word` / voice event (ESPHome)
- [ ] UDP relay device ⇄ EL WS; audio convert/resample
- [ ] EL API key from HA secrets
- [ ] Wire `client_tool_call` → HA service calls
- [ ] Wire wolt: bridge → local wolt MCP (stdio) → result
- [ ] (optional) hermes actions via MCP

## Phase 3 — Firmware: `el_agent` ESPHome component (in the fork)
- [ ] Scaffold `esphome/components/el_agent/` from `voice_assistant`
- [ ] Start on `micro_wake_word`; stream XMOS-clean mic over UDP to the bridge
- [ ] Play incoming UDP audio via the existing mixer/speaker path
- [ ] Keep playback on the XMOS AEC-reference path (verify no echo)
- [ ] Build + flash a test unit

## Phase 4 — Integration & tuning
- [ ] Full path: wake → bridge → EL → tools → audio back, end to end
- [ ] Validate barge-in (talk over the agent) — AEC quality
- [ ] Agent greets on activation ("привет котик, слушаю тебя")
- [ ] Latency tuning; graceful session start/stop/timeout
- [ ] Fallback: keep stock Assist as a rollback path

## Phase 5 — Tools, persona, polish
- [ ] wolt ordering via tools
- [ ] HA control surface (lights, AC, timers, scenes)
- [ ] Telegram for long/complex info
- [ ] Persona (anime-neko, "ня") via system prompt + voice
- [ ] Cost monitoring (EL per-minute usage)

## Open questions (track answers in docs/architecture.md)
- [ ] Exact ElevenLabs Agent WS audio format + event schema
- [ ] Wake→connect handshake: device-initiated UDP vs HA-pushed start
- [ ] Client tools vs EL-native MCP for wolt (public exposure?)
- [ ] AEC reference path in `el_agent`
