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
| (this repo) | Umbrella: docs, the `el_bridge` HA integration, checklist | ours |
| `external/home-assistant-voice-pe` | Voice PE firmware **fork** — home of the custom `el_agent` ESPHome component | fork of `esphome/home-assistant-voice-pe` |
| `external/voice-kit-xmos-firmware` | XMOS XU316 DSP/AEC firmware **fork** — reference for the AEC pipeline | fork of `esphome/voice-kit-xmos-firmware` |
| `hermes-assist-bridge/` | The current **turn-based** brain (HA Assist → hermes-agent) + install docs — reference/fallback | ours |

External (not vendored here): **wolt-mcp** (the Wolt ordering MCP server — kept in a
**separate private repo** since it reverse-engineers a private API for real paid orders),
**ElevenLabs Agents** (cloud).

## Status

🟡 **Scaffolding / design.** No bridge or firmware code yet. See
[`docs/checklist.md`](docs/checklist.md) for the phased plan. Phase 1 is a standalone
bridge prototype (laptop, no hardware) to validate the ElevenLabs Agent WebSocket
audio format, tool protocol, and Russian before touching firmware.

## Security

**No secrets in this repo.** API keys / tokens live in environment variables or
Home Assistant secrets. See `AGENTS.md` for the rules.
