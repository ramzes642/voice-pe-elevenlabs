# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, etc.) and humans working in this repo.
Read this first.

## What this project is

We are replacing the Home Assistant Voice PE's turn-based Assist pipeline with a
**full-duplex realtime voice agent on ElevenLabs Agents**. See `README.md` for the
big picture and `docs/architecture.md` for details.

Three things get built:
1. **`el_agent`** — a custom ESPHome component (lives in the `home-assistant-voice-pe`
   fork under `esphome/components/el_agent/`). On `micro_wake_word`, it streams the
   XMOS-cleaned mic to the bridge over UDP and plays returned audio — full-duplex.
2. **`el_bridge`** — a Home Assistant custom integration (this repo, `bridge/`). It
   relays the device's UDP audio ⇄ the ElevenLabs Agent WebSocket, holds the API key,
   converts audio formats, and executes ElevenLabs `client_tool_call`s (HA service
   calls, wolt MCP, etc.).
3. **ElevenLabs Agent config** — agent + tools + voice + (optional) custom LLM.

## Repo layout

```
README.md                  overview + architecture
AGENTS.md                  this file
docs/
  architecture.md          detailed flow, ports, audio formats, tool handling
  hardware.md              Voice PE / XMOS XU316 facts (verified from firmware source)
  checklist.md             phased task list — keep it updated
bridge/                    el_bridge: ElevenLabs Phase-1 probe (HA integration not started)
rtbridge/                  realtime bridge on the Pi: Voice PE ⇄ OpenAI Realtime (WORKING, systemd)
hwtest/                    laptop-side acoustic tests of the real device (ping_pong.py, dialog.py)
sun-wakeword/              «солнце моё» wake word: training + firmware YAML + patched components
Makefile                   make flash / make bridge-deploy / make test
external/
  home-assistant-voice-pe/ fork (submodule) — firmware + el_agent component
  voice-kit-xmos-firmware/ fork (submodule) — XMOS DSP/AEC reference
```

## Conventions

- **Bridge**: Python 3.13, `asyncio` + `aiohttp`/`websockets`. Audio as raw PCM frames.
- **Firmware**: ESPHome (YAML + C++ external component). Build with `esphome compile`.
  Min ESPHome version per the fork's `home-assistant-voice.yaml` (currently 2026.5.0).
- Match the surrounding code style of whatever component you edit (ESPHome C++ idioms
  in the fork; idiomatic async Python in `bridge/`).
- Keep `docs/checklist.md` in sync when you finish or add a task.

## Hard rules

- **Never commit secrets.** No ElevenLabs API keys, HA tokens, Wolt tokens, refresh
  tokens, or `.env` files. Use env vars / HA secrets. `.gitignore` covers `.env`.
- The user's tokens/keys are **their own**, provided for their own account — but they
  still must not land in git or be printed.
- **Do not push, fork, create repos, or restart the user's live services without
  explicit per-action approval.** This is personal home infrastructure.
- Prefer **client tools** (executed by the bridge locally) over exposing local MCP
  servers to the ElevenLabs cloud, unless the user opts into public exposure.

## Key hardware facts (don't re-derive — see docs/hardware.md)

- Voice PE = **ESP32-S3** (networking, micro_wake_word, ESPHome) + **XMOS XU316**
  (xcore.ai DSP: AEC / interference-cancel / noise-suppress / AGC, 2 mic channels).
- Mic: I2S, 16 kHz, stereo, continuous. Speaker: I2S 48 kHz via AIC3204 DAC.
- **Full-duplex already works** in stock firmware: micro_wake_word + the "stop" word
  listen during playback (XMOS AEC cancels the speaker). We reuse this.
- Device ⇄ HA transport is the **ESPHome native API / UDP**, not raw Wyoming.

## Where to start

Current state (2026-10-03): the колонка already holds live full-duplex conversations via
`rtbridge/` (OpenAI Realtime) on **stock firmware + two small patches** (see
`rtbridge/README.md`): no custom `el_agent` component was needed. Firmware builds on the
Pi with `make flash`; the bridge deploys with `make bridge-deploy`; test from the laptop
with `hwtest/dialog.py`. The firmware/HA facts above still hold; the ElevenLabs path
(`bridge/`, Phases 2–3) is on hold. Next work: tools (HA actions, wolt) as Realtime
function calls, latency tuning. Keep `docs/checklist.md` in sync.

Operational rules for the device: never send `TTS_END` / `STT_VAD_END` / `STT_END` to the
device mid-session (mic stops); never STOP an announcement younger than ~1.5 s (device
decoder crash); the HTTP audio stream is paced to real time for that reason.
