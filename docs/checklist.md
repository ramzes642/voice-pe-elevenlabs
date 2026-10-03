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

## Hardware test harness (`hwtest/`, laptop ⇄ real Voice PE over air + USB log)
- [x] `ping_pong.py`: laptop says «Солнце моё» + command, records mic, captures ESPHome log over
      USB, local whisper per-second timeline, PASS/FAIL verdict (validated 2026-10-03: wake +1.4s,
      STT +2.8s, answer +4.3s after command on stock fw v14 + hermes bridge)
- [x] `realtime_openai.py`: OpenAI Realtime session (laptop mic/speakers); `dialog.py`: scripted multi-turn
      acoustic test (phrase, wait, phrase, …) with per-second timeline
- [ ] Stage 2 driver: OpenAI Realtime model plays the human (multi-turn, judges answers)
- [x] Root `Makefile`: `make flash` = sync YAML+model → compile on the Pi (ESPHome docker) → OTA → artifacts;
      `make flash-usb` (esptool over the USB cable), `make logs`, `make test`
- [ ] Re-run `ping_pong.py` against el_agent firmware once Phase 3 exists; compare timings

## Realtime dialog on the real device (`rtbridge/`, 2026-10-03)  ✅ WORKING
Goal: «солнце моё» → live full-duplex conversation through the колонка, no HA in the loop.
- [x] Stock-firmware path found: device streams mic over the native API for as long as no
      STT_END/RUN_END arrives; responses play via media_player announcement (HTTP) → full duplex
- [x] Firmware: patched `voice_assistant` (later API subscriber takes over, `rtbridge*` cannot be
      displaced), announcement pipeline `format: NONE` (WAV codec) → fw v17 (`make flash`)
- [x] `rtbridge` service on the Pi (systemd, enabled): aioesphomeapi VA server + chunked-WAV HTTP +
      OpenAI Realtime WS (gpt-realtime-2.1, semantic VAD, far-field NR). Mic = ch1 (no AGC, no echo) x16
- [x] Stage 1 loopback (echo of the mic through the device while mic keeps streaming) — verified
- [x] Stage 2 live dialog: answer starts ~1.6–2.1 s after end of question; 14 s answer plays fully
- [x] Barge-in: playback cut ~1.1 s after the user starts talking; new question answered
- [x] `end_conversation` tool (needs the user's quoted farewell, bridge verifies it) → goodbye →
      RUN_END → device idle. Device-side «stop» / wake word also end the session. Idle timeout 25 s
- [x] Device crash on STOP-right-after-start (double free in decoder task) avoided: HTTP stream paced
      to real time (+0.6 s lead), STOP only for announcements older than 1.5 s
- [x] HA custom integration `custom_components/rtbridge`: config flow (ESPHome device picker + OpenAI key),
      options (model/voice/persona/agent/timeouts/gain/eagerness), `home_assistant(command)` tool →
      `conversation.process` with the chosen agent (built-in intents or hermes). `make ha-deploy` done,
      imports verified inside the HA container
- [x] HA restarted, integration added from the UI (device picked from ESPHome entries, key from
      <config>/rtbridge.env), standalone systemd bridge disabled. Verified by voice 2026-10-03:
      «включи/выключи свет в гостиной» → built-in agent `action_done` in 30 ms → agent confirms;
      «спасибо, пока» → end_conversation → device idle
- [x] HA restarted with: echo guard (mute mic 0.8 s at announcement start), no filler before tool
      calls, farewell guard waits for the transcript, translation fix
- [x] Device crash on every HA reconnect found and fixed: stock `api` sends a VoiceAssistantConfigurationResponse
      with a null `active_wake_words` pointer to a non-owner client → LoadProhibited. Patched copy in
      `sun-wakeword/firmware/components/api/` (fw v18). Worth an upstream PR to esphome/esphome.
- [x] fw v18 + HA restart verified: no crash on HA reconnect; «включи/выключи свет» → tool fires at once,
      «Готово, свет выключен» ~3 s after the user stops; farewell ends the session
- [x] Greeting off by default (collided with users who speak right after the wake word), echo guard 0.5 s
- [x] Stuck-turn watchdog: semantic VAD once stalled 10 s after a complete phrase; if no response
      starts 2 s after speech_stopped the bridge commits the buffer and requests a response
- [ ] Tune first-response latency (semantic VAD eagerness), persona, wolt as a tool
- [ ] Fallback to HA when rtbridge is down (patched fw keeps no fallback pointer — HA gets the
      device back only on its next reconnect)

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
