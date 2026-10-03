# rtbridge — Voice PE ⇄ OpenAI Realtime, full duplex, no Home Assistant in the loop

Runs on the Raspberry Pi. After «солнце моё» the колонка streams its microphone to this
service continuously; the service holds an OpenAI Realtime WebSocket, streams the model's
voice back to the device, and lets you interrupt the model mid-sentence.

```
Voice PE ──native API (VoiceAssistantAudio, 16 kHz s16le)──▶ rtbridge ──pcm16 24 kHz──▶ OpenAI Realtime
Voice PE ◀──media_player announce: http://pi:8766/stream/<id>.wav (chunked WAV 24 kHz)── rtbridge ◀── audio deltas
```

## How it works (and why no custom firmware component was needed)

* We connect to the device's **ESPHome native API** (`aioesphomeapi`) and subscribe as its
  voice assistant, exactly like Home Assistant does. On wake, the device sends
  `VoiceAssistantRequest(start)`; we answer "port 0" = send audio over the API, and the
  device keeps streaming mic chunks for as long as it stays in `STREAMING_MICROPHONE` —
  i.e. until the server sends `STT_VAD_END` / `STT_END` / `RUN_END`. We send none of those
  during a session, so the mic never stops.
* Response audio goes through the device's **media player as an announcement** (HTTP URL
  served by this process). The announcement mixer input is independent of the microphone
  state, and the XMOS AEC cancels it from the mic → true full duplex. One URL per model
  response; barge-in = `media_player STOP` + the model's own server-VAD cancellation.
* Stock ESPHome lets the **first** API client that subscribes own the voice assistant and
  rejects the rest — HA always wins. The firmware ships with a patched `voice_assistant`
  (`sun-wakeword/firmware/components/voice_assistant/`): a later subscriber takes over, the
  displaced one is kept as a fallback and restored when the new owner disconnects, and a
  client whose `client_info` starts with `rtbridge` cannot be displaced. HA stays connected
  and gets the device back whenever rtbridge is down.

Session ends when: the model calls the `end_conversation` tool (after saying goodbye), the
user says the wake word or «stop» (device-side `voice_assistant.stop`), nobody speaks for
`RTBRIDGE_IDLE_TIMEOUT` seconds, or `RTBRIDGE_MAX_SESSION` is reached. We then send
`RUN_END` → device back to idle, LEDs reset, wake word armed.

Device events we send for LEDs: `RUN_START`+`STT_START` (waiting), `STT_VAD_START` (user
speaking), `TTS_START{text}` (replying; also arms the «stop» wake word). Never `TTS_END`
mid-session — it would switch the device to `STREAMING_RESPONSE` and silence the mic.

## Tuning knobs that mattered (measured 2026-10-03)

| What | Setting | Why |
|---|---|---|
| mic channel | `RTBRIDGE_MIC_CHANNEL=1` (data2, no AGC) | ch0 (AGC) carries the device's own voice at −10 dB → false barge-ins; ch1 has it at −50 dB |
| mic gain | `RTBRIDGE_MIC_GAIN=16` | ch1 speech peaks at 0.03 FS; the model's VAD truncated phrases |
| turn detection | `RTBRIDGE_VAD=semantic` (eagerness auto) | server_vad at 0.7 s silence split «Привет! …» into two turns |
| noise reduction | `far_field` (session.audio.input.noise_reduction) | residual echo + room |
| stream pacing | `RTBRIDGE_STREAM_LEAD=0.6` s | device buffers only 0.6 s ahead → closing the stream ≈ stop |
| STOP policy | only if announcement ≥1.5 s old | STOP right after start crashed the device (double free) |
| end tool | `end_conversation(quote)` verified against transcript | model hallucinated a farewell on an empty turn |

Timings seen: OpenAI session ready 0.8–1.8 s after wake (mic is buffered meanwhile); answer
audio starts 1.6–2.1 s after the user stops; barge-in cuts playback ~1.1 s after speech starts.

## Deploy / run

```bash
make bridge-deploy          # rsync + venv on the Pi (~/rtbridge)
scp hwtest/.env rpi:~/rtbridge/rtbridge/.env     # OPENAI_API_KEY etc. (never in git)
make bridge-install         # systemd unit, enabled
make bridge-restart / bridge-logs / bridge-stop
make bridge-loopback        # stage-1 check: echoes 4 s of what the device heard back to it
```

Watch a conversation: `make bridge-logs` (USER:/AGENT: transcripts, tool calls, session ends).
Recordings of each session (stereo: L=ch0, R=ch1) land in `~/rtbridge/rec/` on the Pi.

Config: `rtbridge/.env.example`. The device's API encryption key is read from the ESPHome
YAML on the Pi (`VOICE_PE_YAML`) unless `VOICE_PE_PSK` is set.

## Files

| File | What |
|---|---|
| `device.py` | native-API client: VA subscription, mic chunks in, events + media player commands out |
| `audio_http.py` | aiohttp server: per-response chunked WAV streams the device pulls |
| `openai_rt.py` | OpenAI Realtime WS session: audio in/out, server VAD, transcripts, tools |
| `main.py` | orchestration: sessions, resampling 16k→24k, barge-in, timeouts, `--mode loopback` |
| `rtbridge.service` | systemd unit |
