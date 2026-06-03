# Checklist

Keep this updated as work progresses. `[ ]` todo, `[~]` in progress, `[x]` done.

## Phase 0 — Scaffolding
- [x] Create umbrella repo (docs, structure)
- [x] Write README / AGENTS.md / architecture / hardware docs
- [x] `gh auth login` (user, interactive)
- [x] Fork `esphome/home-assistant-voice-pe` → add as submodule `external/`
- [x] Fork `esphome/voice-kit-xmos-firmware` → add as submodule `external/`
- [x] Create **public** GitHub repo + push → https://github.com/ramzes642/voice-pe-elevenlabs

## Phase 1 — Bridge prototype (laptop, NO hardware)  ← start here
De-risk ElevenLabs before touching firmware.
- [ ] Create an ElevenLabs Agent in the dashboard (Russian, voice, system prompt/persona)
- [ ] Get `agent_id`; put `ELEVENLABS_API_KEY` in a local `.env` (gitignored)
- [ ] `bridge/prototype.py`: open the Agent WebSocket, stream a wav / laptop mic in,
      play the agent's audio out
- [ ] Confirm: **audio format** (sample rate / codec, in & out)
- [ ] Confirm: **Russian** STT+TTS quality end-to-end
- [ ] Confirm: **client_tool_call / client_tool_result** schema with a dummy tool
      (e.g. `turn_on_ac`)
- [ ] Measure round-trip latency

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
