# rtbridge — Home Assistant custom integration

The same bridge as `../../rtbridge/`, running **inside Home Assistant**, configured from the UI,
and with Home Assistant itself as a tool the voice agent can call.

```
«солнце моё» → Voice PE streams mic (native API) → rtbridge (in HA) ⇄ OpenAI Realtime
                                                      │ response audio: HA HTTP view /api/rtbridge/stream/<id>.wav → device media player
                                                      └ tool home_assistant("включи свет на кухне") → conversation.process (agent of your choice)
```

## Install

1. `make ha-deploy` copies `custom_components/rtbridge/` into HA's config dir on the Pi and
   import-checks it inside the container. Restart Home Assistant.
2. Settings → Devices & services → Add integration → **Realtime Voice Bridge (OpenAI)**.
   Pick the Voice PE from the list of ESPHome devices (host + encryption key come from its
   ESPHome entry, nothing to type) and paste the OpenAI API key.
3. Stop the standalone service if it is running: `make bridge-stop` (and `sudo systemctl disable rtbridge`).
   Both subscribe as `rtbridge*`; the last one to subscribe wins, so run only one.

Needs firmware with the patched `voice_assistant` + WAV codec (`make flash`, fw ≥ v17).

## Options (⚙ Configure)

| Option | Default | Notes |
|---|---|---|
| Realtime model / Voice | `gpt-realtime-2.1` / `marin` | |
| Agent instructions | Russian persona + tool rules | tells the model to route smart-home requests to `home_assistant` |
| Let the agent control Home Assistant | on | adds the `home_assistant(command)` tool |
| Conversation agent that executes commands | `conversation.home_assistant` | the built-in agent handles «включи свет…», timers, scenes, states over **exposed entities** (Settings → Voice assistants → Expose). Pick `conversation.hermes_assist` to route commands to hermes instead |
| Language | `ru` | passed to the agent and to transcription |
| Greet on wake | on | «Слушаю!» right after the wake word |
| Turn detection eagerness | auto | semantic VAD; `high` answers faster, `low` lets you pause |
| End session after silence / max length | 25 s / 600 s | |
| Microphone gain | 16 | the AEC channel without AGC is quiet |
| Audio base URL | HA internal URL | must be reachable by the device over LAN (http) |

Changing options reloads the entry.

## How commands work

The Realtime model does not get your entity list. It calls `home_assistant(command)` with a
plain Russian phrase; the bridge runs `conversation.process` with the chosen agent and returns
`{response_type, speech}`; the model then speaks the outcome in its own words. With the
built-in agent this is fully local and instant (intents over exposed entities, aliases, areas);
with hermes you get the agentic brain (MCP tools, wolt, …) at the cost of its latency. The
conversation_id is kept for the session so follow-ups («а теперь выключи») work.

Session ends on: the model's `end_conversation(quote)` (quote must appear in the transcript),
the wake word or «stop» said to the device, idle timeout, max length.

## Files

`__init__.py` setup/unload · `config_flow.py` UI flows · `bridge.py` sessions + tools ·
`device.py` ESPHome native-API voice-assistant server · `http.py` chunked WAV view ·
`openai_rt.py` Realtime WS on aiohttp · `const.py` defaults
