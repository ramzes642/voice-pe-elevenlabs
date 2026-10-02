# hwtest — testing the real Voice PE from this laptop

Laptop-side tools for exercising the actual колонка acoustically: the laptop plays the
wake word and commands through its speakers, listens to the device's answer through its
mic, and reads the device's ESPHome log over USB for exact event timing.

```
hwtest/
├── ping_pong.py        stage 1: «Солнце моё» … «Это пинг, ответь понг» → timeline + verdict
├── realtime_openai.py  stage 2: bidirectional realtime session with OpenAI Realtime (mic ⇄ speakers)
├── tts.py              phrase synthesis (macOS `say -v Milena` or ElevenLabs), cached in cache/
├── devlog.py           Voice PE log over USB serial, timestamped + parsed into events
├── transcribe.py       local faster-whisper with word timestamps → per-second timeline
├── .env.example        copy to .env, fill keys (gitignored)
└── runs/<ts>/          session.wav, device.log, events.json, timeline.txt, report.json
```

## Setup

```bash
cd hwtest
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env            # fill keys as needed (see below)
```

Plug the Voice PE into the laptop via USB-C. It enumerates as Espressif
"USB JTAG_serial debug unit" → `/dev/cu.usbmodem*`, auto-detected. Keep the laptop within
a metre of the device; the device's answer is ~25 dB quieter in the laptop mic than the
laptop's own playback (the transcriber normalizes each window separately for that).

## Which keys

| Key | Needed for | Default without it |
|---|---|---|
| none | `ping_pong.py` | macOS `say -v Milena` for phrases, local whisper for transcription |
| `ELEVENLABS_API_KEY` | `--tts elevenlabs` (other voices/prosody for the test phrases) | `say` |
| `OPENAI_API_KEY` | `realtime_openai.py` (stage 2 realtime talk) | — |

## Stage 1 — ping/pong

```bash
.venv/bin/python ping_pong.py
.venv/bin/python ping_pong.py --command "сколько времени" --expect ""     # just look
.venv/bin/python ping_pong.py --pause 0.5 --tts elevenlabs --whisper small
.venv/bin/python ping_pong.py --list-devices
```

What it does, in order: starts mic recording + USB log capture → plays the wake word →
pauses (`--pause`, default 1 s) → plays the command → waits until the device log says the
answer finished playing (or `--listen` timeout) → saves `session.wav` → transcribes with
faster-whisper (`--whisper large-v3-turbo` by default, ~15 s on an M-series CPU; `small` is
quick and rough) → prints a per-second timeline with device events aligned → verdict.

Checks: wake word detected; device STT matches the command; expected word in the device's
response text; expected word heard by the laptop mic. Exit code 0 = PASS.

Reference timing measured 2026-10-03 (stock firmware v14 + hermes-assist-bridge):

| Step | Δt |
|---|---|
| wake word start → `micro_wake_word` detection | +1.37 s (phrase itself is 0.9 s) |
| end of command → STT VAD end (silence timeout) | +1.8 s |
| → STT text | +2.8 s |
| → response text (hermes) | +4.25 s |
| → device starts speaking | +4.25 s |

Note: say the wake word only when the device is idle; if it is still playing a previous
answer, `micro_wake_word` fires but the pipeline does not start.

## Stage 2 — OpenAI Realtime

`realtime_openai.py` holds a `RealtimeSession`: mic → `input_audio_buffer.append` (pcm16
24 kHz) and `response.output_audio.delta` → speakers, server VAD, barge-in flush, input and
output transcripts logged with timestamps. Standalone it is a sanity check of key + audio
path; imported, it lets the model play the human in front of the колонка (instructions:
say the wake word, ask things, judge answers), with `on_event` for logging.

```bash
.venv/bin/python realtime_openai.py --first "Привет, скажи что-нибудь" --duration 20
```

Protocol notes (from the docs, 2026-10): `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`,
`Authorization: Bearer`, `session.update` with `session.type: "realtime"`,
`session.audio.input.format {type: audio/pcm, rate: 24000}`,
`session.audio.input.turn_detection {type: server_vad | semantic_vad}`,
`session.audio.output.voice`. No `OpenAI-Beta` header (GA).

## Standalone transcription

```bash
.venv/bin/python transcribe.py runs/<ts>/session.wav --split 4.9   # normalize before/after split separately
```
