# hermes-assist-bridge

A small HTTP bridge that lets **Home Assistant Assist** use a [hermes-agent] as its
conversation agent. This is the **turn-based** brain currently running the колонка
(STT in HA → this bridge → hermes-agent with tools/MCP → TTS in HA).

It is the predecessor to the full-duplex ElevenLabs approach this repo is building toward
— kept here for reproducibility and as a working fallback.

> Not affiliated with ElevenLabs; lives in this repo only because it's part of the same
> voice-assistant effort.

## What it does
- Exposes `POST /api/chat` (Bearer-auth) — HA's custom conversation integration sends
  `{text, conversation_id, ...}`, gets back `{reply}`.
- Runs the hermes-agent **in-process** (`USE_DIRECT_AGENT=1`) for low latency, falling back
  to the `hermes` CLI if the in-process agent fails.
- Extras baked in (see `bridge.py`):
  - **MCP auto-registration** for direct-agent mode (reads `mcp_servers:` from the
    hermes `config.yaml`), so tools like `wolt` work without the CLI path.
  - **System prompt**: anime-neko persona + native HA timer instruction + "send long/complex
    info to Telegram" instruction.
  - **Tool-click sound** (debounced) played on the Voice PE when tools run.
  - **Session reset** after `SESSION_RESET_S` idle; trims history.
  - Sets `continue_conversation` when the reply ends in a question (handled HA-side).
- `GET /health` — status (model, provider, toolsets, agent_ready).

## Files
| File | Goes to |
|---|---|
| `bridge.py` | `/opt/hermes-assist-bridge/bridge.py` |
| `bridge.env.example` | copy to `/etc/hermes-assist-bridge/bridge.env` and edit |
| `hermes-assist-bridge.service` | `/etc/systemd/system/` |

`api.key` and the real `bridge.env` are **secrets** — never commit (gitignored here).

## Prerequisites
- A working **hermes-agent** install (default `HERMES_REPO=/usr/local/lib/hermes-agent`,
  `hermes` CLI on PATH), configured with a model/provider and (optionally) MCP servers in
  its `config.yaml`.
- `python3` with `pyyaml` (for MCP config parsing) available to the agent's environment.
- Home Assistant reachable; `HASS_URL` + `HASS_TOKEN` placed in the hermes env file
  (`HERMES_ENV_FILE`, default `/root/.hermes/.env`).

## Install
```bash
sudo mkdir -p /opt/hermes-assist-bridge /etc/hermes-assist-bridge
sudo cp bridge.py /opt/hermes-assist-bridge/

# config
sudo cp bridge.env.example /etc/hermes-assist-bridge/bridge.env
sudo nano /etc/hermes-assist-bridge/bridge.env      # set HOST, model, toolsets, etc.

# bearer key (kept out of git)
head -c 32 /dev/urandom | base64 | sudo tee /etc/hermes-assist-bridge/api.key >/dev/null
sudo chmod 600 /etc/hermes-assist-bridge/api.key

# HASS_URL / HASS_TOKEN live in the hermes env file
echo 'HASS_URL=https://your-ha.example' | sudo tee -a /root/.hermes/.env
echo 'HASS_TOKEN=<long-lived-token>'   | sudo tee -a /root/.hermes/.env

# service
sudo cp hermes-assist-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-assist-bridge.service
curl -s localhost:8765/health | python3 -m json.tool
```

## Key config (`bridge.env`)
- `HERMES_ASSIST_HOST` / `_PORT` — bind address/port (e.g. a Tailscale IP, or `0.0.0.0`).
- `HERMES_ASSIST_MODEL`, `HERMES_ASSIST_REASONING` — e.g. `gpt-5.4-mini`, `low`.
- `HERMES_ASSIST_TOOLSETS` — comma list (homeassistant,memory,web,wolt,messaging,…).
- `HERMES_ASSIST_USE_DIRECT_AGENT=1` — in-process agent (needed for MCP registration here).
- `HERMES_ASSIST_TOOL_CLICK` / `_CLICK_MEDIA` / `_CLICK_ENTITY` — tool-run click sound on the
  Voice PE (set `_CLICK_ENTITY` to your media_player entity).
- `HERMES_ASSIST_SESSION_RESET_S`, `_MAX_HISTORY_MESSAGES`, `_MAX_TURNS`, `_TIMEOUT`.

## Sounds
`sounds/double-click-computer-mouse.wav` is the tool-run click. Home Assistant serves its
config `www/` directory at `/local/`, so copy the file there:

```bash
cp sounds/double-click-computer-mouse.wav <ha-config>/www/double-click-computer-mouse.wav
```

That makes it reachable at `/local/double-click-computer-mouse.wav`, matching the default
`HERMES_ASSIST_CLICK_MEDIA=/local/double-click-computer-mouse.wav` in `bridge.env`. Played
on `HERMES_ASSIST_CLICK_ENTITY` (your Voice PE media_player) when a tool runs; debounced by
`HERMES_ASSIST_CLICK_MIN_INTERVAL_S`. Set `HERMES_ASSIST_TOOL_CLICK=0` to disable.

## Home Assistant side (not included here)
A small **custom conversation integration** on the HA host posts to this bridge:
`POST http://<HERMES_ASSIST_HOST>:<PORT>/api/chat` with `Authorization: Bearer <api.key>`
and `{ "text": ..., "conversation_id": ... }`, then returns `reply` to Assist. (That
component lives on the HA host; add it for full reproducibility if needed.)

## Note
This bridge is **turn-based** (HA's Assist pipeline). The realtime, full-duplex direction —
streaming audio straight to an ElevenLabs Agent — is what the rest of this repo targets; see
`../docs/architecture.md`.

[hermes-agent]: internal project (the agent runtime this bridge drives)
