# voice-pe-elevenlabs

Full-duplex realtime voice agent for the **Home Assistant Voice PE** ("колонка"),
powered by the **ElevenLabs Agents Platform**, replacing Home Assistant's
turn-based Assist pipeline (STT → conversation → TTS) with a single streaming
speech-to-speech session.

Goal: **sub-second, streaming, barge-in-capable Russian voice control + ordering**,
where the latency no longer scales with how long you speak.

## Why

The stock pipeline is batch and turn-based:
- STT (whisper) processes the **whole** utterance only after you stop → latency ∝ length.
- Then a separate LLM round-trip, then TTS.

We measured this end-to-end (see `docs/`): moving STT to a fast box helped, but the
structural fix is a **streaming, full-duplex agent**. ElevenLabs Agents gives us:
streaming STT + LLM + best-in-class TTS + turn-taking + **tools/function-calling**,
over a WebSocket, with Russian support — and we already have an ElevenLabs account.

## Target architecture

```
[Voice PE — custom firmware]
  micro_wake_word (on-device)  ──fires──▶  el_agent component
  XMOS-AEC mic   ──UDP audio──▶  ┐
  speaker        ◀──UDP audio──  │
                                 ▼
                    [Home Assistant: custom integration  "el_bridge"]
                      • sees the device's wake event (ESPHome)
                      • holds the ElevenLabs API key (HA secrets)
                      • converts/resamples audio (16k PCM ⇄ EL format)
                      • holds the WebSocket  ──▶  [ElevenLabs Agent]
                                                    STT + LLM + TTS + turn-taking + barge-in
                                                    tools ──▶ client_tool_call ──▶ back to el_bridge
                                                              └─ HA service calls / wolt MCP (local) / hermes (MCP)
```

hermes is **not** in the audio path. If hermes actions are needed, they are reached
as **tools** (client-tool handled by the bridge, or EL-native MCP).

See [`docs/architecture.md`](docs/architecture.md) for the detailed flow, ports,
audio formats and open questions.

## Repos in this project

| Path | What | Origin |
|---|---|---|
| (this repo) | Umbrella: docs, the `el_bridge` HA integration, test tooling, checklist | ours |
| `bridge/` | `el_bridge`: Phase 1 probe (`probe.py`) today, HA custom integration later | ours |
| `custom_components/rtbridge/` | **The same bridge as a Home Assistant integration**: configured from the HA UI, HA commands via the `home_assistant` tool (`conversation.process` with any agent) | ours |
| `rtbridge/` | Standalone realtime bridge on the Pi (systemd): Voice PE native API (mic in, media player out) ⇄ OpenAI Realtime — the first working version, kept for HA-less use | ours |
| `hwtest/` | Laptop-side harness for the **real** Voice PE: plays wake word + phrases, records the answer, aligns with the device's USB log, local whisper timeline (`ping_pong.py`, `dialog.py`) | ours |
| `sun-wakeword/` | Russian wake word **«солнце моё»** for `micro_wake_word`: training pipeline, trained models, the firmware YAML currently flashed | ours |
| `hermes-assist-bridge/` | The current **turn-based** brain (HA Assist → hermes-agent) + install docs — reference/fallback | ours |
| `Makefile` | `make flash`: build the firmware on the Raspberry Pi (ESPHome docker) and OTA it to the колонка | ours |
| `external/home-assistant-voice-pe` | Voice PE firmware **fork** — home of the custom `el_agent` ESPHome component | fork of `esphome/home-assistant-voice-pe` |
| `external/voice-kit-xmos-firmware` | XMOS XU316 DSP/AEC firmware **fork** — reference for the AEC pipeline | fork of `esphome/voice-kit-xmos-firmware` |

External (not vendored here): **wolt-mcp** (the Wolt ordering MCP server — kept in a
**separate private repo** since it reverse-engineers a private API for real paid orders),
**ElevenLabs Agents** (cloud).

Submodules are not fetched by default:

```bash
git submodule update --init --recursive
```

## Status

- ✅ **Phase 0 / 1** — scaffolding; ElevenLabs Agent validated from a laptop: `pcm_16000` in/out,
  Russian STT/TTS, `client_tool_call` round-trip, ≈0.28 s end-of-speech → first audio.
- ✅ **Live full-duplex dialog on the real колонка** (`rtbridge/`, 2026-10-03) — «солнце моё» →
  conversation with **OpenAI Realtime** (`gpt-realtime-2.1`) through the device: answers start
  ~2 s after you stop talking, you can interrupt mid-sentence, «спасибо, пока» ends the session.
  Runs as a systemd service on the Raspberry Pi; HA stays connected but is not in the loop.
  Firmware v17 = stock + patched `voice_assistant` subscription + WAV codec (`make flash`).
- 🟡 **HA integration** (`custom_components/rtbridge/`) — same bridge inside Home Assistant with a config
  flow (pick the ESPHome device, paste the OpenAI key) and an options flow (model, voice, persona, which
  conversation agent executes smart-home commands: built-in intents or hermes). Deployed with `make ha-deploy`,
  **Running**: light commands verified by voice («включи свет в гостиной» → HA built-in agent, 30 ms).
- 🟡 **Next** — latency tuning; wolt as a tool; decide whether the ElevenLabs path (`bridge/`) is still needed.
  See [`docs/checklist.md`](docs/checklist.md).

## Working with the real device

The Voice PE sits on the LAN (`192.168.68.83`) and, when plugged into the laptop over USB-C,
exposes its ESPHome log on `/dev/cu.usbmodem*`. Home Assistant and the ESPHome builder run on a
Raspberry Pi (`rpi` in `~/.ssh/config`).

```bash
make flash            # sync YAML + patched components + wake-word model → compile in docker → OTA → bins
make logs             # live ESPHome log over USB
make test             # hwtest/ping_pong.py: «Солнце моё» … «Это пинг, ответь понг» → per-second timeline + verdict
make bridge-deploy    # rsync rtbridge/ to the Pi + venv;  make bridge-restart / bridge-logs / bridge-stop
make ha-deploy        # copy custom_components/rtbridge into HA's config dir (+ import check in the container)
```

Scripted conversation from the laptop (phrase, seconds to wait, phrase, …):

```bash
cd hwtest && .venv/bin/python dialog.py "Солнце моё" 1.5 "Привет! Почему небо голубое?" 6 "Спасибо, пока!" 8
```

`hwtest/` needs its own venv (`cd hwtest && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`);
no API keys are required for the ping/pong test. Details in [`hwtest/README.md`](hwtest/README.md),
firmware/retraining details in [`sun-wakeword/README.md`](sun-wakeword/README.md).

## Security

**No secrets in this repo.** API keys / tokens live in environment variables or
Home Assistant secrets. See `AGENTS.md` for the rules.
