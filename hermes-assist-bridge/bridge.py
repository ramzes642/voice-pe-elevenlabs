#!/usr/bin/env python3
"""Small HTTP bridge from Home Assistant Assist to Hermes Agent.

Endpoints:
- GET /health
- POST /api/chat with an Authorization header containing the bridge API key

This bridge is optional. HACS installs only the Home Assistant custom
integration; run this bridge separately on the machine that has Hermes Agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("HERMES_ASSIST_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERMES_ASSIST_PORT", "8765"))
KEY_FILE = Path(os.environ.get("HERMES_ASSIST_KEY_FILE", "./hermes-assist-bridge.key"))
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
HERMES_REPO = os.environ.get("HERMES_REPO", "")
HERMES_ENV_FILE = os.environ.get("HERMES_ENV_FILE", "")
TIMEOUT = int(os.environ.get("HERMES_ASSIST_TIMEOUT", "120"))
MAX_PROMPT_CHARS = int(os.environ.get("HERMES_ASSIST_MAX_PROMPT_CHARS", "6000"))
MAX_HISTORY_MESSAGES = int(os.environ.get("HERMES_ASSIST_MAX_HISTORY_MESSAGES", "8"))
USE_DIRECT_AGENT = os.environ.get("HERMES_ASSIST_USE_DIRECT_AGENT", "0").lower() in {"1", "true", "yes"}
MODEL = os.environ.get("HERMES_ASSIST_MODEL", "")
PROVIDER = os.environ.get("HERMES_ASSIST_PROVIDER", "")
TOOLSETS = [x.strip() for x in os.environ.get("HERMES_ASSIST_TOOLSETS", "").split(",") if x.strip()]
MAX_TURNS = int(os.environ.get("HERMES_ASSIST_MAX_TURNS", "8"))
REASONING_EFFORT = os.environ.get("HERMES_ASSIST_REASONING", "minimal")
# Drop stale conversation history if a conversation has been idle longer than
# this (seconds): the agent starts clean instead of being anchored to earlier
# answers (e.g. from before tools/skills were enabled).
SESSION_RESET_SECONDS = int(os.environ.get("HERMES_ASSIST_SESSION_RESET_S", "300"))
_CONV_LAST_SEEN: dict[str, float] = {}
_CONV_LOCK = threading.Lock()

SYSTEM_PROMPT = """You are a cute anime catgirl voice assistant (нэко-тян) talking through Home Assistant Assist with your master (call him as any cute animal like "зайчик", "котик", "солнышко", "красавчик", "малыш"). Always end your conversation with question-mark if the conversation looks like incomplete.
Persona: be cheerful, warm and a little playful. Speak Russian. Naturally sprinkle cute interjections like "ня", "ня~", "нян", "мяу", "ехехе" into your replies — especially at the start or end — and use affectionate diminutives. Stay endearing but DON'T overdo it: always give the real, correct answer first; the cuteness is seasoning, not the whole dish.
This is voice / text-to-speech: output ONLY plain speakable words. No emoji, no kaomoji, no asterisks, no symbols and no action descriptions — they get read aloud and sound wrong.
Keep replies concise, practical, and speech-friendly. Answer in Russian unless the user clearly asks for another language.
Use the recent conversation context to resolve pronouns and follow-up questions. U
You are the primary voice assistant for this Home Assistant setup. For smart-home requests, use your available Home Assistant tools directly when appropriate instead of redirecting the user to another agent. For delivery queries use only home address (kolossi), do not clarify location.
Safety: do not unlock doors, disable alarms, open gates/garage doors, delete/send email, or make broad smart-home changes unless the spoken request is explicit and unambiguous. If unsure, ask a short clarifying question.
"""

_AGENT = None
_AGENT_LOCK = threading.Lock()
_AGENT_READY = False
_AGENT_ERROR: str | None = None


def load_env_file(path: str) -> None:
    """Load simple KEY=VALUE secrets into the bridge process environment."""
    if not path:
        return
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


load_env_file(HERMES_ENV_FILE)

# --- "agent is working" click: play a short sound on each tool call ---
import urllib.request as _urlreq

HASS_URL = os.environ.get("HASS_URL", "").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
CLICK_ENTITY = os.environ.get(
    "HERMES_ASSIST_CLICK_ENTITY", "media_player.home_assistant_voice_xxxxxx_media_player"
)
CLICK_MEDIA = os.environ.get("HERMES_ASSIST_CLICK_MEDIA", "/local/double-click-computer-mouse.mp3")
CLICK_ENABLED = (
    os.environ.get("HERMES_ASSIST_TOOL_CLICK", "1").lower() in {"1", "true", "yes"}
    and bool(HASS_URL and HASS_TOKEN)
)


CLICK_MIN_INTERVAL = float(os.environ.get("HERMES_ASSIST_CLICK_MIN_INTERVAL_S", "1.0"))
_CLICK_LAST = [0.0]
_CLICK_RL_LOCK = threading.Lock()


def _play_tool_click() -> None:
    if not CLICK_ENABLED:
        return
    # Rate-limit: never more than one click per CLICK_MIN_INTERVAL seconds —
    # overlapping media on the Voice PE reboots it.
    with _CLICK_RL_LOCK:
        _now = time.time()
        if _now - _CLICK_LAST[0] < CLICK_MIN_INTERVAL:
            return
        _CLICK_LAST[0] = _now

    def _fire() -> None:
        try:
            body = json.dumps(
                {
                    "entity_id": CLICK_ENTITY,
                    "media_content_id": CLICK_MEDIA,
                    "media_content_type": "music",
                    "announce": True,
                }
            ).encode()
            req = _urlreq.Request(
                HASS_URL + "/api/services/media_player/play_media",
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + HASS_TOKEN,
                    "Content-Type": "application/json",
                },
            )
            resp = _urlreq.urlopen(req, timeout=3)
            print("[tool-click] play_media ->", resp.status, flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort, never block the agent
            print("[tool-click] error:", repr(exc), flush=True)

    threading.Thread(target=_fire, daemon=True).start()


def _on_tool_start(_call_id=None, _name=None, _args=None) -> None:
    _play_tool_click()


# --- Inline skill injection: bake selected skills' text into the system prompt
# so the agent follows them directly (no skills_list/skill_view round-trips).
INJECT_SKILLS = [
    x.strip()
    for x in os.environ.get("HERMES_ASSIST_INJECT_SKILLS", "").split(",")
    if x.strip()
]


def _load_injected_skills() -> str:
    if not INJECT_SKILLS:
        return ""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    skills_root = os.path.join(home, "skills")
    found_texts = []
    for name in INJECT_SKILLS:
        skill_md = None
        for root, dirs, files in os.walk(skills_root, followlinks=True):
            if os.path.basename(root) == name and "SKILL.md" in files:
                skill_md = os.path.join(root, "SKILL.md")
                break
        if not skill_md:
            continue
        try:
            txt = open(skill_md, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if txt.startswith("---"):
            end = txt.find("\n---", 3)
            if end != -1:
                txt = txt[end + 4:]
        found_texts.append("## Built-in skill: " + name + "\n" + txt.strip())
    if not found_texts:
        return ""
    return (
        "\n\nYou have the following built-in skills baked in. When a request matches "
        "one, FOLLOW ITS STEPS DIRECTLY using your available tools (e.g. cronjob, "
        "homeassistant) and actually perform the actions — never claim you lack the "
        "capability, and do not look for separate timer helpers.\n\n"
        + "\n\n".join(found_texts)
        + "\n"
    )


SYSTEM_PROMPT = SYSTEM_PROMPT + _load_injected_skills()
SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\n\nTIMERS: to set a countdown timer (e.g. 'поставь таймер на 5 минут'), run the "
    "Home Assistant service timer.start on entity_id timer.voice_timer with data "
    "{\"duration\": \"HH:MM:SS\"} — e.g. \"00:05:00\" for 5 minutes, \"00:00:30\" for 30 "
    "seconds, \"01:20:00\" for 1h20m. This is the native Home Assistant timer and it chimes "
    "automatically when finished, so just start it and confirm briefly and cutely. To cancel a "
    "timer, run timer.cancel on timer.voice_timer.\n"
)

SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\n\nTELEGRAM: the user's phone Telegram is connected. For LONG or COMPLEX content "
    "(menus, lists of choices, order summaries, links, several prices/addresses — anything "
    "awkward to convey by voice), do NOT read it all aloud: send it to the user with the "
    "send_message tool (target=\"telegram\", message=<the full content>), then give a SHORT "
    "spoken confirmation like 'Отправила в телеграм, ня'. Use plain text for Telegram (it can "
    "be longer and include links/lists).\n"
)


def load_key() -> str:
    try:
        return KEY_FILE.expanduser().read_text().strip()
    except FileNotFoundError:
        return os.environ.get("HERMES_ASSIST_API_KEY", "").strip()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _content_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                txt = part.get("text") or part.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts).strip()
    return ""


def format_history(data: dict[str, Any], current_text: str) -> str:
    """Extract concise previous chat context from Home Assistant's Assist chat log."""
    chat_log = data.get("chat_log")
    if not isinstance(chat_log, dict):
        return ""
    items = chat_log.get("content")
    if not isinstance(items, list):
        return ""

    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _content_text(item)
        if not text:
            continue
        if role == "user" and text.strip().lower() == current_text.strip().lower():
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {text[:600]}")

    if not lines:
        return ""
    return "Recent conversation, oldest to newest:\n" + "\n".join(lines[-MAX_HISTORY_MESSAGES:]) + "\n"


def get_agent():
    """Create one in-process Hermes agent and reuse it across voice requests."""
    global _AGENT, _AGENT_READY, _AGENT_ERROR
    if _AGENT is not None:
        return _AGENT
    with _AGENT_LOCK:
        if _AGENT is not None:
            return _AGENT
        try:
            if HERMES_REPO:
                sys.path.insert(0, HERMES_REPO)
                os.chdir(HERMES_REPO)
            from hermes_constants import parse_reasoning_effort
            from run_agent import AIAgent

            kwargs: dict[str, Any] = {
                "enabled_toolsets": TOOLSETS or None,
                "max_iterations": MAX_TURNS,
                "quiet_mode": True,
                "skip_context_files": True,
                "skip_memory": True,
                "reasoning_config": parse_reasoning_effort(REASONING_EFFORT),
                "platform": "homeassistant",
            }
            if CLICK_ENABLED:
                kwargs["tool_start_callback"] = _on_tool_start
            if MODEL:
                kwargs["model"] = MODEL
            if PROVIDER:
                kwargs["provider"] = PROVIDER
            # The in-process agent does not auto-load MCP servers (run_agent has
            # no MCP support). Register them ourselves from config.yaml's
            # mcp_servers: section so direct-agent mode keeps wolt/ordering tools.
            try:
                from tools.mcp_tool import register_mcp_servers as _reg_mcp
                import yaml as _y

                class _NoTags(_y.SafeLoader):
                    pass

                _NoTags.add_multi_constructor("!", lambda _l, _s, _n: None)
                _cfgp = os.path.join(
                    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                    "config.yaml",
                )
                _mcp = (_y.load(open(_cfgp, encoding="utf-8"), Loader=_NoTags) or {}).get("mcp_servers") or {}
                if _mcp:
                    _names = _reg_mcp(_mcp)
                    print("[mcp] registered servers", list(_mcp), "-> tools:", _names, flush=True)
            except Exception as _mexc:  # noqa: BLE001 - MCP is best-effort
                print("[mcp] register failed:", repr(_mexc), flush=True)

            _AGENT = AIAgent(**kwargs)
            _AGENT_READY = True
            _AGENT_ERROR = None
            return _AGENT
        except Exception as exc:  # noqa: BLE001 - fallback to CLI path
            _AGENT_ERROR = str(exc)
            _AGENT_READY = False
            raise


def call_hermes_direct(prompt: str) -> tuple[bool, str]:
    """Call the in-process Hermes agent. Returns (ok, text)."""
    try:
        agent = get_agent()
        with _AGENT_LOCK:
            return True, (agent.chat(prompt) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def call_hermes_cli(prompt: str) -> tuple[bool, str]:
    """Fallback CLI call, slower but simple and robust."""
    cmd = [HERMES_BIN, "chat", "-q", prompt, "--source", "home-assistant-assist", "-Q", "--max-turns", str(MAX_TURNS)]
    if PROVIDER:
        cmd += ["--provider", PROVIDER]
    if MODEL:
        cmd += ["-m", MODEL]
    if TOOLSETS:
        cmd += ["--toolsets", ",".join(TOOLSETS)]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "Hermes exited with an error")[-2000:]
    return True, (proc.stdout or "").strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesAssistBridge/1.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "service": "hermes-assist-bridge", "time": time.time(), "direct_agent": USE_DIRECT_AGENT, "agent_ready": _AGENT_READY, "agent_error": _AGENT_ERROR, "model": MODEL, "provider": PROVIDER, "toolsets": TOOLSETS, "max_turns": MAX_TURNS, "reasoning": REASONING_EFFORT})
        else:
            json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            json_response(self, 404, {"error": "not_found"})
            return

        expected = load_key()
        auth = self.headers.get("Authorization", "")
        if not expected or auth != f"Bearer {expected}":
            json_response(self, 401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 1024 * 1024))
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            json_response(self, 400, {"error": "bad_json", "detail": str(exc)})
            return

        text = str(data.get("text", "")).strip()
        if not text:
            json_response(self, 400, {"error": "missing_text"})
            return

        conversation_id = str(data.get("conversation_id") or "")
        language = str(data.get("language") or "")
        device_id = str(data.get("device_id") or "")
        extra_system_prompt = str(data.get("extra_system_prompt") or "")
        _now = time.time()
        _reset = False
        if conversation_id:
            with _CONV_LOCK:
                _last = _CONV_LAST_SEEN.get(conversation_id)
                if _last is not None and (_now - _last) > SESSION_RESET_SECONDS:
                    _reset = True
                _CONV_LAST_SEEN[conversation_id] = _now
        history = "" if _reset else format_history(data, text)

        prompt = (
            f"{SYSTEM_PROMPT}\n"
            f"Home Assistant context:\n"
            f"- conversation_id: {conversation_id or 'none'}\n"
            f"- language: {language or 'unknown'}\n"
            f"- device_id: {device_id or 'unknown'}\n"
        )
        if extra_system_prompt:
            prompt += f"- extra Home Assistant instruction: {extra_system_prompt[:1000]}\n"
        if history:
            prompt += f"\n{history}"
        prompt += f"\nUser said: {text}\n\nReply with only the answer the user should hear."
        prompt = prompt[:MAX_PROMPT_CHARS]

        ok, output = call_hermes_direct(prompt) if USE_DIRECT_AGENT else call_hermes_cli(prompt)
        if USE_DIRECT_AGENT and not ok:
            ok, output = call_hermes_cli(prompt)

        if not ok:
            if output == "timeout":
                json_response(self, 504, {"error": "timeout", "reply": "Sorry, Hermes took too long to answer."})
            else:
                json_response(self, 502, {"error": "hermes_failed", "detail": output[-2000:], "reply": "Sorry, Hermes failed."})
            return

        output = output.strip() or "Hermes did not return a response."
        json_response(self, 200, {"reply": output, "conversation_id": conversation_id or None})


def main() -> None:
    if USE_DIRECT_AGENT:
        try:
            get_agent()
        except Exception as exc:  # noqa: BLE001
            print(f"Direct Hermes agent unavailable, CLI fallback will be used: {exc}", file=sys.stderr, flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Hermes Assist Bridge listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
