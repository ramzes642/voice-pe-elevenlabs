# Architecture

## End-to-end flow

```
                          on-device                         LAN / tailscale            cloud
┌───────────────────────────────────────────┐      ┌────────────────────────┐    ┌──────────────────┐
│ Voice PE                                    │      │ Home Assistant          │    │ ElevenLabs Agent │
│                                             │      │  (custom integration    │    │                  │
│  mics ─▶ XMOS XU316 (AEC/IC/NS/AGC) ─▶ I2S  │      │   "el_bridge")          │    │  STT (Scribe)    │
│                       │                     │      │                         │    │  LLM (own/custom)│
│              ┌────────┴────────┐            │      │  • wake event (ESPHome) │    │  TTS (Flash v2.5)│
│              │ micro_wake_word │            │      │  • EL API key (secret)  │    │  turn-taking     │
│              └────────┬────────┘            │      │  • audio convert/resample    │  barge-in        │
│                  fires│                     │      │  • client_tool exec     │    │  tools           │
│              ┌────────▼────────┐  UDP PCM   │      │                         │ WS │                  │
│              │  el_agent (NEW) │ ─────────▶ │─────▶│  ── relay ──▶           │───▶│                  │
│              │  full-duplex    │ ◀───────── │◀─────│  ◀── relay ──           │◀───│                  │
│              └────────┬────────┘  UDP PCM   │      │                         │    │                  │
│                  speaker ◀ mixer ◀ AIC3204  │      │  client_tool_call ◀─────┼────┤ (function call)  │
└───────────────────────────────────────────┘      │   └▶ HA service / wolt MCP / hermes (MCP)        │
                                                     └────────────────────────┘    └──────────────────┘
```

## Components & responsibilities

### 1. `el_agent` (ESPHome custom component, in the firmware fork)
- Triggered by `micro_wake_word` on-device (wake stays local: fast, private, free).
- Streams the **XMOS-AEC-cleaned mic** channel to the bridge over UDP, continuously,
  for the whole session (not gated per turn).
- Receives audio frames from the bridge and plays them through the **existing speaker
  path** (mixer → AIC3204) so the XMOS keeps using it as the **AEC reference**.
- Built by forking/trimming ESPHome's stock `voice_assistant` component, which already
  has: UDP audio mode, continuous mode, simultaneous capture+playback, `on_audio()`.

### 2. `el_bridge` (Home Assistant custom integration, this repo)
- Subscribes to the device's `micro_wake_word` / voice event via ESPHome.
- On wake: opens a WebSocket to the ElevenLabs Agent; tears it down on end/timeout.
- Relays audio both ways and **converts format** (device 16 kHz PCM ⇄ EL's expected
  input; EL output ⇄ 48 kHz for the speaker).
- Holds the **ElevenLabs API key** (HA secrets) — the device never sees it.
- Handles ElevenLabs **`client_tool_call`** events: executes them locally and returns
  `client_tool_result`. Tool targets: HA service calls; the local wolt MCP (stdio);
  hermes via MCP if needed.
- Phase 1 lives as a **standalone script** (laptop mic/wav in, speaker out) to validate
  ElevenLabs before HA/firmware exist.

### 3. ElevenLabs Agent (cloud, configured in their dashboard)
- STT (Scribe, Russian) → LLM → TTS (Flash v2.5, ~75 ms, Russian), turn-taking, barge-in.
- LLM: built-in (Claude/GPT/Gemini) **or** custom LLM (OpenAI-compatible endpoint) if we
  want to keep our own brain.
- Tools defined here; preferred = **client tools** (executed by `el_bridge`).

## Audio (CONFIRMED in Phase 1)
- Device mic: 16 kHz, mono after XMOS, 16-bit PCM (XMOS outputs 2 channels; we use the
  AEC/ASR one).
- **ElevenLabs Agent WS: pcm_16000 both input and output** (mono, 16-bit). So the device
  mic feeds in with **no resampling**; the agent's 16 kHz output is upsampled to 48 kHz by
  the firmware's existing resampler/mixer for the speaker.
- Input: stream `user_audio_chunk` (base64 pcm16k) frames; server-side VAD ends the turn on
  trailing silence. Give a short lead-in so the first word isn't clipped.
- **Measured latency: end-of-speech → first agent audio ≈ 0.28 s.**

## ElevenLabs WS protocol (CONFIRMED in Phase 1)
- Endpoint `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=…`, auth `xi-api-key` header.
- Client→server: `conversation_initiation_client_data`, `user_audio_chunk` (base64),
  `user_message` (text), `pong`, `client_tool_result`.
- Server→client: `conversation_initiation_metadata` (audio formats + conversation_id),
  `audio` (`audio_event.audio_base_64`), `user_transcript`, `agent_response`,
  `agent_response_correction`, `ping`, `client_tool_call`, `interruption`, `vad_score`.
- Tool call: `{tool_name, tool_call_id, parameters, event_id, expects_response}` → reply
  `{type:"client_tool_result", tool_call_id, result, is_error}` (only consumed when
  `expects_response`).
- Dashboard edits require **Publish** to reach the API.

## Tool handling — two options
1. **Client tools (preferred):** EL → `client_tool_call` → `el_bridge` executes locally.
   - HA actions: direct HA service call inside the integration. Nothing exposed publicly.
   - wolt: bridge invokes the **local** wolt MCP (stdio) and returns the result.
   - Keeps everything private; bridge is the executor.
2. **EL-native MCP:** EL cloud connects directly to an MCP server.
   - Requires the MCP server to be **internet-reachable** (public URL/tunnel + auth).
   - Simpler EL config, larger attack surface. Use only if the user opts in.

## Open questions / gotchas
- **AEC reference:** verify `el_agent` plays through the same output path the XMOS uses
  as the echo reference, or barge-in echo returns.
- ~~**EL WS protocol:** audio format + tool schema + VAD~~ — RESOLVED in Phase 1 (see above).
- **Wake → connect handshake:** does `el_agent` open the UDP stream directly (firmware
  knows the bridge endpoint) with the bridge reacting to the wake event, or does HA push
  a start to the device? Likely device-initiated on wake; HA reacts.
- ~~**Latency budget**~~ — measured ≈0.28 s end-of-speech→first audio (cloud, from a laptop).
  Re-measure once device→HA(LAN)→EL path exists.
- **Cost:** EL Agents billed per minute (~$0.08–0.10/min platform + LLM). Track usage.
