# Hardware & firmware facts (Home Assistant Voice PE)

Verified from the firmware source (`esphome/home-assistant-voice-pe`,
`home-assistant-voice.yaml` + `esphome/components/voice_kit/`) and the ESPHome core
`voice_assistant` component.

## Chips
- **ESP32-S3** — wifi/networking, `micro_wake_word`, ESPHome runtime, native API.
- **XMOS XU316** (xcore.ai) — dedicated audio DSP. 16 logical cores across 2 tiles,
  ~2400 MIPS / 1200 MFLOPS, vector unit; designed for far-field voice. Runs XMOS
  **FFVA** ("Far-Field Voice Assistant") firmware, flashed via the `voice_kit` component
  (`ffva_v1.3.1` at time of writing). **This is a real hardware AEC** — same family as
  (and newer than) the XVF3000 used in high-end conference mics.

## XMOS DSP pipeline (configurable per channel over I2C from the ESP32)
From `esphome/components/voice_kit/voice_kit.h`:
```
PIPELINE_STAGE_NONE=0  AEC=1  IC=2 (interference cancel)  NS=3 (noise suppress)  AGC=4
```
- 2 microphone channels (0 and 1); each can be assigned a pipeline stage.
- The ESP32 (`voice_kit` component) sets/reads these stages.

## Audio I/O (`home-assistant-voice.yaml`)
- **Microphone** `i2s_mics`: I2S input, **16 kHz, stereo, 32-bit**, continuous capture.
  - `micro_wake_word` consumes channel 1; `voice_assistant` consumes channels 0 and 1.
- **Speaker** `i2s_audio_speaker`: I2S out, **48 kHz**, via **AIC3204** DAC.
  - Virtual `mixer` speaker combines announcement + media streams; `resampler` speakers
    upsample to 48 kHz; ducking (−20 dB) applied while listening.
- I2S pins: output LRCLK GPIO7 / BCLK GPIO8 / DOUT GPIO10; input LRCLK GPIO14 /
  BCLK GPIO13 / DIN GPIO15.

## Full-duplex is already exercised (key finding)
- `micro_wake_word` starts on connect and **never stops** (`stop_after_detection: false`)
  → the mic is processed continuously, regardless of playback.
- During a response >1 s, `activate_stop_word_once` enables the **"stop"** wake word, so
  the mic is analysed **while TTS/media is playing** → say "okay nabu"/"stop" to
  interrupt. That is simultaneous speaker-out + mic-in, made reliable by the XMOS AEC.
- ⇒ The hardware + lower firmware layers support full-duplex today. The *turn-based*
  behaviour is just the stock `voice_assistant` state machine, which we replace.

## Device ⇄ HA transport
- ESPHome **native API** (`api:` block) — protobuf over the HA connection — with two
  audio modes in `voice_assistant`: `AUDIO_MODE_API` (over the API conn) or
  `AUDIO_MODE_UDP` (separate UDP socket). **Not raw Wyoming.** (Wyoming is HA ⇄
  external STT/TTS/wake services.)
- `voice_assistant` states (turn-based):
  `IDLE → START_MICROPHONE → STREAMING_MICROPHONE → STOP_MICROPHONE → AWAITING_RESPONSE
   → STREAMING_RESPONSE → RESPONSE_FINISHED`.
- Continuous streaming exists: `StartContinuousAction` / `set_continuous()` — this is the
  legacy "online wake word" mode (stream continuously, server-side openWakeWord). Proof
  that continuous upstream streaming is built in.

## Implications for `el_agent`
- Reuse the existing continuous + UDP + simultaneous-playback plumbing from
  `voice_assistant`; trigger on `micro_wake_word`; keep playback on the XMOS-referenced
  output path so AEC keeps cancelling the speaker.
