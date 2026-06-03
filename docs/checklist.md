# Checklist

Keep this updated as work progresses. `[ ]` todo, `[~]` in progress, `[x]` done.

## Phase 0 — Scaffolding
- [x] Create umbrella repo (docs, structure)
- [x] Write README / AGENTS.md / architecture / hardware docs
- [x] `gh auth login` (user, interactive)
- [x] Fork `esphome/home-assistant-voice-pe` → add as submodule `external/`
- [x] Fork `esphome/voice-kit-xmos-firmware` → add as submodule `external/`
- [x] Create **public** GitHub repo + push → https://github.com/ramzes642/voice-pe-elevenlabs

## Phase 1 — Bridge prototype (laptop, NO hardware)  ✅ COMPLETE
De-risked ElevenLabs before touching firmware.
- [x] ElevenLabs Agent "Nyan-cat" (gemini-2.5-flash), **language=ru**, neko persona, voice VD1if7jD…
      — note: dashboard changes require **Publish** to take effect.
- [x] `agent_id` + `ELEVENLABS_API_KEY` in `bridge/.env` (gitignored, verified untracked)
- [x] `bridge/probe.py`: REST config fetch + WS round-trips (text / audio / multi-turn `--tool`)
- [x] **Audio format**: pcm_16000 both input & output
- [x] **Russian**: accurate Scribe STT from audio ("Включи кондиционер во всём доме."),
      Russian TTS + neko persona ("Привет котик! Ня~"). Needs a ~0.6s lead-in so the first word
      isn't clipped, and a settle delay after init.
- [x] **client_tool_call / client_tool_result**: `turn_on_ac` fired, we replied, agent continued.
      Schema: `{tool_name, tool_call_id, parameters:{}, event_id, expects_response}`.
- [x] Feed **real audio** (TTS-generated) → STT validated, not just text
- [x] **Latency**: end-of-input → first agent audio ≈ **0.28 s** 🚀

Protocol confirmed (see docs/architecture.md): WS `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=…`,
auth via `xi-api-key` header; events `conversation_initiation_metadata` (carries audio formats + conversation_id),
`agent_response`, `agent_response_correction`, `audio` (`audio_event.audio_base_64`), `ping`→`pong`,
`client_tool_call`→`client_tool_result`, `user_transcript`, `interruption`; send
`user_message`(text) / `user_audio_chunk`(base64 pcm16k). Turn end = server VAD on trailing silence.

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
