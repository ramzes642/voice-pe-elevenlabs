"""Sessions: wake word on the Voice PE → OpenAI Realtime conversation, with HA as a tool."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import numpy as np
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import (DEFAULT_COMMAND_AGENT, DEFAULT_IDLE_TIMEOUT, DEFAULT_INSTRUCTIONS, DEFAULT_LANGUAGE,
                    DEFAULT_MAX_SESSION, DEFAULT_MIC_GAIN, DEFAULT_MODEL, DEFAULT_VAD_EAGERNESS, DEFAULT_VOICE,
                    CONF_API_KEY, CONF_HOST, CONF_NOISE_PSK, OPT_AUDIO_BASE_URL, OPT_COMMAND_AGENT, OPT_GREETING,
                    OPT_HA_TOOL, OPT_IDLE_TIMEOUT, OPT_ECHO_GUARD, DEFAULT_ECHO_GUARD, DEFAULT_GREETING, OPT_TURN_STALL, DEFAULT_TURN_STALL, OPT_PLAYBACK_DUCK, DEFAULT_PLAYBACK_DUCK, OPT_INSTRUCTIONS, OPT_LANGUAGE, OPT_MAX_SESSION, OPT_MIC_GAIN,
                    OPT_MODEL, OPT_VAD_EAGERNESS, OPT_VOICE, STREAM_PATH)
from .device import VoicePE
from .http import Stream, new_stream
from .openai_rt import RealtimeSession

log = logging.getLogger(__name__)
MIC_RATE, OAI_RATE = 16000, 24000

TOOL_END = {
    "type": "function", "name": "end_conversation",
    "description": "Завершить голосовую сессию и перевести колонку в режим ожидания ключевого слова. "
                   "Вызывай ТОЛЬКО если пользователь ЯВНО попрощался или попросил закончить («пока», «спасибо, всё», "
                   "«хватит», «отбой», «замолчи»). Никогда не вызывай при пустой или неразборчивой реплике. "
                   "Сначала коротко попрощайся.",
    "parameters": {"type": "object", "properties": {
        "quote": {"type": "string", "description": "дословные слова пользователя, которыми он попрощался"}},
        "required": ["quote"]},
}
TOOL_HA = {
    "type": "function", "name": "home_assistant",
    "description": "Умный дом Home Assistant: включить/выключить/настроить свет, розетки, климат, шторы, медиа, "
                   "сцены, таймеры, узнать состояние датчиков и устройств. Передай просьбу пользователя одной "
                   "русской фразой в повелительном наклонении или вопросом, например «включи свет на кухне», "
                   "«поставь таймер на 5 минут», «какая температура в спальне». Ответ перескажи коротко.",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "команда или вопрос для умного дома, по-русски"}},
        "required": ["command"]},
}


def _stem_pattern(word: str) -> str:
    """Regex matching Russian declensions of `word`: keep the first len-2 letters (min 3), allow any tail."""
    w = word.lower().replace("ё", "е")
    keep = max(3, len(w) - 2) if len(w) > 3 else len(w)
    return re.escape(w[:keep]) + r"[\w-]*"


def normalize_areas(command: str, area_names: list[str]) -> str:
    """«включи свет на кухне» → «включи свет на Кухня».

    HA's Russian intents match area names literally (`[в|на] {area}`), without morphology, so a
    declined form never hits the area. Replace any declined form of a known area with its exact
    name (longest names first so «Задний двор» wins over «Двор»)."""
    out = command.replace("ё", "е")
    for name in sorted(area_names, key=len, reverse=True):
        words = name.split()
        if not words:
            continue
        pat = r"\b" + r"\s+".join(_stem_pattern(w) for w in words) + r"\b"
        out = re.sub(pat, name, out, flags=re.IGNORECASE)
    return out


def exposed_inventory(hass: HomeAssistant, limit: int = 120) -> str:
    """One line per entity exposed to Assist: «Имя (домен, зона) [алиасы]». Lets the model phrase
    commands with the exact names HA matches literally, instead of guessing declensions."""
    areas = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    lines = []
    for state in hass.states.async_all():
        eid = state.entity_id
        domain = eid.split(".")[0]
        if domain in ("sensor", "binary_sensor", "update", "button", "event", "number", "select", "device_tracker"):
            continue
        if not async_should_expose(hass, "conversation", eid):
            continue
        ent = ent_reg.async_get(eid)
        area_id = (ent.area_id if ent else None) or (dev_reg.async_get(ent.device_id).area_id if ent and ent.device_id and dev_reg.async_get(ent.device_id) else None)
        name = state.name
        aliases = sorted(a for a in (ent.aliases or ()) if isinstance(a, str)) if ent else []
        if domain == "scene":
            line = f"сцена «{name}» ({areas.get(area_id, 'без зоны')}) — только «активируй сцену {name}»"
        elif domain in ("automation", "script"):
            line = f"{domain} «{name}» ({areas.get(area_id, 'без зоны')})"
        else:
            line = f"{name} ({domain}, {areas.get(area_id, 'без зоны')}) сейчас: {state.state}"
        if aliases:
            line += " алиасы: " + ", ".join(aliases)
        lines.append(line)
        if len(lines) >= limit:
            break
    return "\n".join(sorted(lines))


def _quote_in(quote: str, text: str) -> bool:
    """True if a meaningful word of the quoted farewell appears in the transcript text."""
    t = text.lower().replace("ё", "е")
    return any(w.strip(",.!?") in t for w in quote.lower().replace("ё", "е").split() if len(w.strip(",.!?")) > 2)


def resample_16k_to_24k(pcm: bytes, state: dict) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) == 0:
        return b""
    prev = state.get("prev")
    if prev is not None:
        x = np.concatenate([[prev], x])
    state["prev"] = x[-1]
    n_out = int(np.floor((len(x) - 1) * 1.5))
    pos = np.arange(n_out) * (2.0 / 3.0)
    i = np.clip(np.floor(pos).astype(np.int64), 0, len(x) - 2)
    frac = (pos - i).astype(np.float32)
    y = x[i] * (1 - frac) + x[i + 1] * frac
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


class Session:
    def __init__(self, bridge: "RtBridge", conversation_id: str, wake_word: str | None):
        self.b = bridge
        self.id = conversation_id or f"s{int(time.time())}"
        self.t0 = time.monotonic()
        self.ending = False
        self.stream: Stream | None = None
        self.oai: RealtimeSession | None = None
        self.rs_state: dict = {}
        self.pending: list[bytes] = []
        self.user_text: list[str] = []
        self.ha_conversation_id: str | None = None
        self.mic_chunks = 0
        self.mute_until = 0.0     # echo guard: ignore the mic for the first moments of each announcement
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        self.b.dev.session_begin()
        self._tasks.append(asyncio.create_task(self._run_safe()))
        self._tasks.append(asyncio.create_task(self._watchdog()))

    async def _run_safe(self):
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session setup failed")
            self.b.dev.error("rtbridge", "session setup failed")
            await self.end("setup exception")

    async def on_audio(self, data: bytes, data2: bytes | None):
        self.mic_chunks += 1
        if data2:
            data = data2          # channel 1: no AGC → the device's own voice is ~40 dB down
        if time.monotonic() < self.mute_until:
            data = bytes(len(data))   # the AEC leaks the first ~0.7 s of a new announcement
        gain = self.b.mic_gain
        # Duck only while *our* response audio is (or just was) playing — the device's wake sound at
        # session start is also an announcement and must not mute the user's first words.
        if self.stream is not None and self.b.dev.recently_playing(0.8):
            gain *= self.b.playback_duck   # residual echo of the device's own voice stays under the VAD
        if gain != 1.0:
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) * gain
            data = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        pcm24 = resample_16k_to_24k(data, self.rs_state)
        if self.oai is None or not self.oai.ready.is_set():
            self.pending.append(pcm24)
            return
        if self.pending:
            for p in self.pending:
                await self.oai.send_audio(p)
            self.pending.clear()
        await self.oai.send_audio(pcm24)

    async def end(self, reason: str):
        if self.ending:
            return
        self.ending = True
        log.info("session %s ending: %s (%.1fs, %d mic chunks)", self.id, reason, time.monotonic() - self.t0, self.mic_chunks)
        if self.stream:
            self.stream.close(discard=True)
        self.b.dev.stop_playback()
        if self.oai:
            await self.oai.close()
        self.b.dev.session_end()
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()

    async def _watchdog(self):
        while not self.ending:
            await asyncio.sleep(0.5)
            if time.monotonic() - self.t0 > self.b.max_session:
                await self.end("max session length"); return
            if self.oai and self.oai.ready.is_set():
                if self.oai.turn_stuck(self.b.turn_stall):
                    await self.oai.force_turn()
                if time.monotonic() - self.oai.last_activity > self.b.idle_timeout:
                    await self.end("idle"); return
                if self.oai.closed.is_set():
                    await self.end("openai connection closed"); return

    async def _run(self):
        dev, hass = self.b.dev, self.b.hass
        agent_text = {"cur": ""}

        def on_audio(pcm: bytes):
            if self.stream is None or self.stream.closed:
                self.stream = new_stream(hass, OAI_RATE)
                dev.agent_speaking(agent_text["cur"] or "…")
                dev.play_url(self.b.base_url + STREAM_PATH.format(sid=self.stream.id))
                self.mute_until = time.monotonic() + self.b.echo_guard
            self.stream.push(pcm)

        def on_audio_done():
            if self.stream:
                self.stream.close()

        def on_speech_started():
            if self.stream and not self.stream.closed:
                self.stream.close(discard=True)
            dev.stop_playback()
            dev.user_speaking()

        def on_user(t: str):
            log.info("USER:  %s", t); self.user_text.append(t)

        def on_agent(t: str):
            log.info("AGENT: %s", t); agent_text["cur"] = ""

        tools = [TOOL_END]
        if self.b.ha_tool:
            inv = exposed_inventory(hass)
            ha_tool = dict(TOOL_HA)
            ha_tool["description"] = (TOOL_HA["description"] +
                " Устройства и зоны, которые знает дом (называй их ТОЧНО этими именами, в именительном падеже, "
                "например «включи Двор освещение» или «выключи свет в Кухня»). Чтобы выключить то, что включал, "
                "используй то же самое имя устройства (не сцену): сцены нельзя выключать. Список:\n" + inv)
            tools.append(ha_tool)
        self.oai = RealtimeSession(async_get_clientsession(hass), self.b.api_key, model=self.b.model, voice=self.b.voice,
                                   instructions=self.b.instructions, tools=tools, language=self.b.language,
                                   eagerness=self.b.eagerness, on_audio=on_audio, on_audio_done=on_audio_done,
                                   on_speech_started=on_speech_started, on_agent_transcript=on_agent,
                                   on_user_transcript=on_user, on_tool=self._tool)
        try:
            t = time.monotonic()
            await self.oai.connect()
            await asyncio.wait_for(self.oai.ready.wait(), 15)
            log.info("openai session ready in %.2fs", time.monotonic() - t)
        except Exception as e:
            log.error("openai connect failed: %s", e)
            dev.error("openai", str(e))
            await self.end("openai connect failed")
            return
        if self.b.greeting:
            await self.oai.request_response("Поздоровайся одним коротким словом или фразой, например «Слушаю!».")

    # ---- tools ------------------------------------------------------------------------
    async def _tool(self, name: str, call_id: str, args: dict):
        if name == "end_conversation":
            quote = (args.get("quote") or "").strip()
            # The transcript of the farewell usually lands *after* the tool call: give it up to 3 s.
            n = len(self.user_text)
            for _ in range(30):
                if len(self.user_text) > n or (self.user_text and _quote_in(quote, self.user_text[-1])):
                    break
                await asyncio.sleep(0.1)
            heard = " ".join(self.user_text[-3:]).lower()
            if len(quote) < 3 or not _quote_in(quote, heard):
                log.warning("end_conversation refused: quote=%r, recent=%r", quote, heard)
                return {"ok": False, "error": "пользователь не прощался — продолжай разговор", "_no_response": True}
            log.info("agent ends the conversation: %r", quote)
            asyncio.get_running_loop().create_task(self._end_after_playback())
            return {"ok": True, "_no_response": True}
        if name == "home_assistant":
            return await self._ha_command(args.get("command") or "")
        return {"error": f"unknown tool {name}", "_no_response": True}

    async def _ha_command(self, command: str) -> dict:
        """Hand the phrase to a HA conversation agent (built-in intents or any other agent)."""
        if not command:
            return {"error": "empty command"}
        areas = [a.name for a in ar.async_get(self.b.hass).async_list_areas() if a.name]
        normalized = normalize_areas(command, areas)
        if normalized != command:
            log.info("area names normalized: %r → %r", command, normalized)
            command = normalized
        data = {"text": command, "language": self.b.language, "agent_id": self.b.command_agent}
        if self.ha_conversation_id:
            data["conversation_id"] = self.ha_conversation_id
        log.info("HA ← %r (agent %s)", command, self.b.command_agent)
        try:
            res = await self.b.hass.services.async_call("conversation", "process", data, blocking=True, return_response=True)
        except Exception as e:
            log.warning("conversation.process failed: %s", e)
            return {"ok": False, "error": str(e)[:200]}
        resp = (res or {}).get("response", {}) if isinstance(res, dict) else {}
        self.ha_conversation_id = (res or {}).get("conversation_id") or self.ha_conversation_id
        speech = resp.get("speech", {}).get("plain", {}).get("speech", "")
        rtype = resp.get("response_type", "")
        out = {"ok": rtype != "error", "response_type": rtype, "speech": speech}
        if rtype == "error":
            out["error_code"] = resp.get("data", {}).get("code")
        else:
            # what HA actually touched, with the state it is in now — so the model can report
            # honestly when a target is unavailable (HA still says action_done in that case)
            # Only flag targets that are unavailable; raw states lag behind (Zigbee reports later)
            # and confused the model into «дом сказал включено, но состояние выключено».
            # HA lists matched targets of several types (entity / area / device / floor); only
            # entities have a state. Checking an area id against the state machine returned None
            # and made every area command look «unavailable».
            targets = resp.get("data", {}).get("success", []) + resp.get("data", {}).get("failed", [])
            out["targets"] = [t.get("name") for t in targets]
            dead = [t.get("name") for t in targets if t.get("type") == "entity"
                    and (st := self.b.hass.states.get(t.get("id", ""))) is not None and st.state == "unavailable"]
            if dead:
                out["ok"] = False
                out["error"] = f"недоступно (unavailable), команда не сработала: {', '.join(dead)}"
        log.info("HA → %s", out)
        return out

    async def _end_after_playback(self):
        for _ in range(100):
            if self.stream is None or self.stream.closed:
                break
            await asyncio.sleep(0.1)
        await self.b.dev.wait_playback_done(20)
        await self.end("agent: end_conversation")


class RtBridge:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass, self.entry = hass, entry
        o = entry.options
        self.api_key = entry.data[CONF_API_KEY]
        self.model = o.get(OPT_MODEL, DEFAULT_MODEL)
        self.voice = o.get(OPT_VOICE, DEFAULT_VOICE)
        self.instructions = o.get(OPT_INSTRUCTIONS) or DEFAULT_INSTRUCTIONS
        self.command_agent = o.get(OPT_COMMAND_AGENT, DEFAULT_COMMAND_AGENT)
        self.language = o.get(OPT_LANGUAGE, DEFAULT_LANGUAGE)
        self.idle_timeout = float(o.get(OPT_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
        self.max_session = float(o.get(OPT_MAX_SESSION, DEFAULT_MAX_SESSION))
        self.greeting = bool(o.get(OPT_GREETING, DEFAULT_GREETING))
        self.mic_gain = float(o.get(OPT_MIC_GAIN, DEFAULT_MIC_GAIN))
        self.eagerness = o.get(OPT_VAD_EAGERNESS, DEFAULT_VAD_EAGERNESS)
        self.ha_tool = bool(o.get(OPT_HA_TOOL, True))
        self.echo_guard = float(o.get(OPT_ECHO_GUARD, DEFAULT_ECHO_GUARD))
        self.turn_stall = float(o.get(OPT_TURN_STALL, DEFAULT_TURN_STALL))
        self.playback_duck = float(o.get(OPT_PLAYBACK_DUCK, DEFAULT_PLAYBACK_DUCK))
        self.base_url = (o.get(OPT_AUDIO_BASE_URL) or get_url(hass, allow_external=False, allow_cloud=False,
                                                                allow_ip=True, require_ssl=False)).rstrip("/")
        self.dev: VoicePE | None = None
        self.session: Session | None = None

    async def start(self):
        self.dev = VoicePE(self.entry.data[CONF_HOST], self.entry.data[CONF_NOISE_PSK],
                           on_start=self._on_start, on_audio=self._on_audio, on_stop=self._on_stop)
        await self.dev.start()
        log.info("rtbridge started for %s (audio via %s, commands → %s)", self.entry.data[CONF_HOST], self.base_url, self.command_agent)

    async def stop(self):
        if self.session:
            await self.session.end("integration unloaded")
        if self.dev:
            await self.dev.stop()

    async def _on_start(self, conversation_id: str, wake_word: str | None):
        if self.session and not self.session.ending:
            await self.session.end("new start from device")
        self.session = Session(self, conversation_id, wake_word)
        await self.session.start()

    async def _on_audio(self, data: bytes, data2: bytes | None):
        if self.session and not self.session.ending:
            await self.session.on_audio(data, data2)

    async def _on_stop(self, aborted: bool):
        if self.session and not self.session.ending:
            await self.session.end("device stop")
