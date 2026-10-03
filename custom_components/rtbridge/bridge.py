"""Sessions: wake word on the Voice PE → OpenAI Realtime conversation, with HA as a tool."""
from __future__ import annotations

import asyncio
import logging
import os
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

from .const import (DEFAULT_COMMAND_AGENT, DEFAULT_IDLE_TIMEOUT, DEFAULT_INSTRUCTIONS, DEFAULT_LANGUAGE, DEFAULT_PERSONA, TOOL_RULES,
                    OPT_END_AFTER_ACTION, DEFAULT_END_AFTER_ACTION, OPT_AGC, DEFAULT_AGC, OPT_RECORD, DEFAULT_RECORD, OPT_AGC_LEVEL, DEFAULT_AGC_LEVEL, OPT_NS_LEVEL, DEFAULT_NS_LEVEL,
                    DEFAULT_MAX_SESSION, DEFAULT_MIC_GAIN, DEFAULT_MODEL, DEFAULT_VAD_EAGERNESS, DEFAULT_VOICE,
                    CONF_API_KEY, CONF_HOST, CONF_NOISE_PSK, OPT_AUDIO_BASE_URL, OPT_COMMAND_AGENT, OPT_GREETING,
                    OPT_HA_TOOL, OPT_IDLE_TIMEOUT, OPT_ECHO_GUARD, DEFAULT_ECHO_GUARD, DEFAULT_GREETING, OPT_TURN_STALL, DEFAULT_TURN_STALL, OPT_PLAYBACK_DUCK, DEFAULT_PLAYBACK_DUCK, OPT_START_MUTE, DEFAULT_START_MUTE, OPT_INSTRUCTIONS, OPT_LANGUAGE, OPT_MAX_SESSION, OPT_MIC_GAIN,
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
TOOL_CLIMATE = {
    "type": "function", "name": "climate_control",
    "description": "Кондиционер / климат (сущности climate из списка): включить (режим cool по умолчанию, heat — "
                   "только если попросили греть), выключить, задать температуру или режим. Для кондиционеров "
                   "используй ТОЛЬКО этот инструмент, не home_assistant.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "точное имя климат-устройства из списка, например «Кондиционер»"},
        "action": {"type": "string", "enum": ["turn_on", "turn_off", "set_temperature", "set_mode"]},
        "mode": {"type": "string", "enum": ["cool", "heat", "auto", "dry", "fan_only"],
                 "description": "режим для turn_on/set_mode (по умолчанию cool)"},
        "temperature": {"type": "number", "description": "целевая температура в °C для set_temperature"}},
        "required": ["name", "action"]},
}
TOOL_DEVICE = {
    "type": "function", "name": "device_control",
    "description": "Включить/выключить ОДНО конкретное устройство из списка по его точному имени (свет, "
                   "розетка, выключатель, хелпер, медиаплеер). Надёжнее, чем home_assistant, когда пользователь "
                   "назвал устройство. Для «весь свет в комнате», таймеров, сцен и вопросов используй home_assistant.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "точное имя или алиас устройства из списка"},
        "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle", "press"],
                   "description": "press — нажать кнопку или запустить скрипт"}},
        "required": ["name", "action"]},
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
    # a declension adds at most ~3 letters; an open tail let «телек» swallow «Телевизор»
    return re.escape(w[:keep]) + r"[\w-]{0,3}"


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


def exposed_inventory(hass: HomeAssistant, limit: int = 120, dup_names: set[str] | None = None) -> str:
    """One line per entity exposed to Assist: «Имя (домен, зона) [алиасы]». Lets the model phrase
    commands with the exact names HA matches literally, instead of guessing declensions."""
    areas = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    lines = []
    for state in hass.states.async_all():
        eid = state.entity_id
        domain = eid.split(".")[0]
        if domain in ("update", "event", "number", "select", "device_tracker"):
            continue
        if not async_should_expose(hass, "conversation", eid):
            continue
        ent = ent_reg.async_get(eid)
        area_id = (ent.area_id if ent else None) or (dev_reg.async_get(ent.device_id).area_id if ent and ent.device_id and dev_reg.async_get(ent.device_id) else None)
        name = state.name
        if domain in ("sensor", "binary_sensor"):
            # read-only, with the current value: lets the model answer «какая температура» directly
            if state.state in ("unknown", "unavailable", ""):
                continue
            unit = state.attributes.get("unit_of_measurement", "")
            lines.append(f"датчик «{name}» ({areas.get(area_id, 'без зоны')}): {state.state} {unit}".rstrip())
            continue
        if domain in ("button", "script"):
            dev = dev_reg.async_get(ent.device_id) if ent and ent.device_id else None
            ctx = f", устройство «{dev.name_by_user or dev.name}»" if dev else ""
            aliases = sorted(a for a in ((ent.aliases if ent else None) or ()) if isinstance(a, str)) if ent else []
            line = (f"кнопка «{name}»{ctx} — device_control(name, press)" if domain == "button"
                    else f"скрипт «{name}» — device_control(name, press)")
            if aliases:
                line += " алиасы: " + ", ".join(aliases)
            if state.state == "unavailable":
                line += " [сейчас недоступна]"
            lines.append(line)
            continue
        aliases = sorted(a for a in (ent.aliases or ()) if isinstance(a, str)) if ent else []
        if domain == "scene":
            line = f"сцена «{name}» ({areas.get(area_id, 'без зоны')}) — только «активируй сцену {name}»"
        elif domain in ("automation", "script"):
            line = f"{domain} «{name}» ({areas.get(area_id, 'без зоны')})"
        else:
            line = f"{name} ({domain}, {areas.get(area_id, 'без зоны')}) сейчас: {state.state}"
            if dup_names and name.lower() in dup_names:
                line += f" — имя не уникально, обязательно добавляй зону: «включи {name} в {areas.get(area_id, '…')}»"
        if aliases:
            line += " алиасы: " + ", ".join(aliases)
        lines.append(line)
        if len(lines) >= limit:
            break
    return "\n".join(sorted(lines))


def exposed_names(hass: HomeAssistant) -> tuple[list[str], set[str]]:
    """Exact names + aliases of exposed entities, and the set of names used by more than one entity."""
    ent_reg = er.async_get(hass)
    names: list[str] = []
    seen: dict[str, int] = {}
    for state in hass.states.async_all():
        eid = state.entity_id
        if eid.split(".")[0] in ("sensor", "binary_sensor", "update", "button", "event", "number", "select", "device_tracker"):
            continue
        if not async_should_expose(hass, "conversation", eid):
            continue
        ent = ent_reg.async_get(eid)
        for n in [state.name] + [a for a in ((ent.aliases if ent else None) or ()) if isinstance(a, str)]:
            if n:
                seen[n.lower()] = seen.get(n.lower(), 0) + 1
                # scene/script/automation names are often verb phrases («Выключить кондиционер»);
                # stem-matching them would rewrite the command's own verb — keep them out
                if eid.split(".")[0] not in ("scene", "script", "automation"):
                    names.append(n)
    dups = {n for n, c in seen.items() if c > 1}
    return names, dups


def normalize_names(command: str, names: list[str]) -> str:
    """«включи люстру» → «включи Люстра»: declined forms of exposed entity names → exact names
    (HA matches names literally). Multi-word names are matched word by word by stem."""
    out = command.replace("ё", "е")
    for name in sorted(set(names), key=len, reverse=True):
        words = name.replace("ё", "е").split()
        if not words or all(len(w) < 4 for w in words):
            continue   # too short to stem safely («1», «ТВ»)
        pat = r"\b" + r"\s+".join(_stem_pattern(w) if len(w) >= 4 else re.escape(w.lower()) for w in words) + r"\b"
        out = re.sub(pat, name, out, flags=re.IGNORECASE)
    return out


def _quote_in(quote: str, text: str) -> bool:
    """True if a meaningful word of the quoted farewell appears in the transcript text."""
    t = text.lower().replace("ё", "е")
    return any(w.strip(",.!?") in t for w in quote.lower().replace("ё", "е").split() if len(w.strip(",.!?")) > 2)


def _fir_lowpass(taps: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass; cutoff relative to the sample rate (0.5 = Nyquist)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n)
    h *= np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


_UP, _DOWN = 3, 2                       # 16 kHz → 48 kHz → 24 kHz
_FIR = _fir_lowpass(60, 0.5 / _UP * 0.92) * _UP   # cutoff just under 8 kHz at 48 kHz; x3 restores level


def resample_16k_to_24k(pcm: bytes, state: dict) -> bytes:
    """s16le 16 kHz → 24 kHz, polyphase-style: zero-stuff x3, FIR low-pass, take every 2nd sample.
    Continuous across chunks (FIR history and decimation phase are kept in `state`). Linear
    interpolation left imaging artefacts that cost the model intelligibility on marginal audio."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) == 0:
        return b""
    up = np.zeros(len(x) * _UP, dtype=np.float32)
    up[::_UP] = x
    hist = state.get("hist")
    if hist is None:
        hist = np.zeros(len(_FIR) - 1, dtype=np.float32)
    sig = np.concatenate([hist, up])
    y = np.convolve(sig, _FIR, mode="valid")          # len == len(up)
    state["hist"] = sig[-(len(_FIR) - 1):]
    phase = state.get("phase", 0)
    out = y[phase::_DOWN]
    state["phase"] = (phase - len(y)) % _DOWN
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


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
        self.pending_end: str | None = None   # end the session once the confirmation has played
        self.pending_end_at = 0.0
        self.pending_end_audio_at = 0.0
        self.last_user_text_at = 0.0
        self.last_agent_text = ""
        self.stream_is_echo_reply = False
        self.agc_env = 8000.0            # ch0 (XMOS AGC) speech sits around 0.25-0.9 FS; start near x2
        self.agc_gain = self.b.mic_gain
        self.rec = None
        self._speex = None
        self._speex_buf = b""
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
        """Mic feed for the model. Two channels arrive from the device: ch0 = the XMOS pipeline's
        ASR output (AEC+IC+NS+AGC — what Home Assistant's own STT uses, good far-field level) and
        ch1 = the same without AGC (very quiet: a voice at 2 m is ~50 LSB, but it carries almost no
        residual of the device's own voice). ch0 leaks our playback back at -10 dB and tripped the
        VAD, so: ch0 while the device is silent, ch1 (fixed gain, ducked) while it is speaking."""
        self.mic_chunks += 1
        now = time.monotonic()
        muted = now < self.mute_until or now - self.t0 < self.b.start_mute
        playing = self.stream is not None and self.b.dev.recently_playing(1.0)   # + reverb tail of «Готово»
        if muted:
            data = bytes(len(data))           # echo guard at announcement start / wake chime
            self.agc_env = 8000.0             # the chime's residual must not set the AGC envelope
        elif playing and data2 and len(data2) == len(data):
            x = np.frombuffer(data2, dtype=np.int16).astype(np.float32) * self.b.mic_gain * self.b.playback_duck
            data = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        elif self.b.agc:
            data = self._agc(data)            # gentle AGC on top of the XMOS AGC (x1..x8)
        if self.b.record:
            self._record(data)
        pcm24 = resample_16k_to_24k(data, self.rs_state)
        if self.oai is None or not self.oai.ready.is_set():
            self.pending.append(pcm24)
            return
        if self.pending:
            # Only the last ~0.3 s of pre-ready audio is worth sending; the rest is the wake chime
            # and its echo, and a big burst at session start degraded the model's hearing of the
            # first utterance.
            for p in self.pending[-10:]:
                await self.oai.send_audio(p)
            self.pending.clear()
        await self.oai.send_audio(pcm24)

    def _agc(self, data: bytes) -> bytes:
        """Fast AGC for the no-AGC mic channel. Instant attack: the gain is computed from the
        current chunk's own peak (so a loud chunk never clips); slow release (~3 s) so pauses
        between words do not pump the noise up; gain clamped to [8, 192]. A person across the room
        is 15-20 dB quieter than the laptop next to the device that the old fixed x16 was fitted to."""
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if len(x) == 0:
            return data
        peak = float(np.abs(x).max())
        if peak > 25:                                   # above the ch1 noise floor
            self.agc_env = peak if peak > self.agc_env else self.agc_env * 0.99 + peak * 0.01
        target = 0.85 * 32767               # OpenAI's VAD ignored speech with peaks ~0.5 (rms ~0.1)
        self.agc_gain = max(self.b.agc_min, min(self.b.agc_max, target / max(self.agc_env, 1.0)))
        return np.clip(x * self.agc_gain, -32768, 32767).astype(np.int16).tobytes()

    def _enhance(self, data: bytes) -> bytes:
        """HA's assist_pipeline audio enhancer: speex AGC (auto_gain 0-31 → x300) + noise suppression
        (0-4 → x-15), on 10 ms frames. The fixed gain alone left far-field speech at -30 dBFS, which
        the server VAD never picked up; speex brings it to a steady level like HA does for Assist."""
        if self._speex is None:
            try:
                from pyspeex_noise import AudioProcessor
                self._speex = AudioProcessor(self.b.agc_level * 300, self.b.ns_level * -15)
            except Exception as e:   # not available (standalone) → fall back to the simple AGC
                log.warning("pyspeex_noise unavailable (%s); using the simple AGC", e)
                self._speex = False
        if self._speex is False:
            return data
        buf = self._speex_buf + data
        out = bytearray()
        n = (len(buf) // 320) * 320
        for i in range(0, n, 320):
            out += self._speex.Process10ms(buf[i:i + 320]).audio
        self._speex_buf = buf[n:]
        return bytes(out)

    def _record(self, data: bytes):
        """Debug: dump the mic feed (after gain) to <config>/rtbridge_rec/<ts>.wav."""
        if self.rec is None:
            self.rec = bytearray()          # collected in memory, written at session end off the loop
        self.rec += data

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
        if self.rec is not None:
            buf, self.rec = bytes(self.rec), None
            path = os.path.join(self.b.hass.config.path("rtbridge_rec"), time.strftime("%Y%m%d_%H%M%S") + ".wav")
            def _write():
                import wave
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with wave.open(path, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(MIC_RATE); w.writeframes(buf)
            await self.b.hass.async_add_executor_job(_write)
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
                # "Silence" = nobody is talking: not the user (VAD), not the model (response in
                # flight), not the speaker (playback, which lags the audio stream by seconds).
                busy = self.oai.in_speech or self.oai.response_active or self.oai.tool_busy or self.b.dev.playing
                quiet_since = max(self.oai.last_activity, self.b.dev.idle_since)
                if not busy and time.monotonic() - quiet_since > self.b.idle_timeout:
                    await self.end("idle"); return
                if self.oai.closed.is_set():
                    await self.end("openai connection closed"); return

    async def _run(self):
        dev, hass = self.b.dev, self.b.hass
        agent_text = {"cur": ""}

        def on_audio(pcm: bytes):
            if self.stream is None or self.stream.closed:
                if self.pending_end and not self.pending_end_audio_at:
                    self.pending_end_audio_at = time.monotonic()   # the confirmation starts playing
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

        def _words(x: str) -> set[str]:
            return {w for w in re.sub(r"[^\w ]+", " ", x.lower().replace("ё", "е")).split() if len(w) > 1}

        def on_user(t: str):
            log.info("USER:  %s", t); self.user_text.append(t)
            # Echo guard: the device's own last sentence coming back through the mic is transcribed as
            # the user («Готово.»). If every word is from the agent's last utterance, it is echo.
            tw = _words(t)
            if tw and tw <= _words(self.last_agent_text) | {"готово", "готова", "ок", "да"}:
                log.info("echo of the agent's own speech ignored: %r", t)
                self.user_text.pop()
                if self.oai is not None:
                    asyncio.get_running_loop().create_task(self.oai.cancel_response())
                    if self.stream and not self.stream.closed and self.stream_is_echo_reply:
                        self.stream.close(discard=True)
                return
            if t.strip():
                self.last_user_text_at = time.monotonic()
            elif self.oai is not None:
                # Empty transcript = a VAD blip (echo tail, noise). The model tends to answer it with
                # a hallucinated «Пока!» + end_conversation: cancel that response and drop its audio.
                asyncio.get_running_loop().create_task(self.oai.cancel_response())
                if self.stream and not self.stream.closed:
                    self.stream.close(discard=True)
                dev.stop_playback()

        def on_agent(t: str):
            log.info("AGENT: %s", t); agent_text["cur"] = ""
            self.last_agent_text = t

        tools = [TOOL_END]
        if self.b.ha_tool:
            _names, dups = exposed_names(hass)
            inv = exposed_inventory(hass, dup_names=dups)
            ha_tool = dict(TOOL_HA)
            ha_tool["description"] = (TOOL_HA["description"] +
                " Значения датчиков в списке актуальны на начало разговора: на вопрос о температуре/влажности/"
                "состоянии отвечай по ним сам, без инструмента. Устройства и зоны, которые знает дом (называй их ТОЧНО этими именами, в именительном падеже, "
                "например «включи Двор освещение» или «выключи свет в Кухня»). Чтобы выключить то, что включал, "
                "используй то же самое имя устройства (не сцену): сцены нельзя выключать. Список:\n" + inv)
            tools.append(ha_tool)
            tools.append(TOOL_DEVICE)
            if any(st.entity_id.startswith("climate.") and async_should_expose(hass, "conversation", st.entity_id)
                   for st in hass.states.async_all()):
                tools.append(TOOL_CLIMATE)
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
            for _ in range(50):
                if len(self.user_text) > n or (self.user_text and _quote_in(quote, self.user_text[-1])):
                    break
                await asyncio.sleep(0.1)
            heard = " ".join(self.user_text[-3:]).lower()
            farewell_words = ("пока", "свидан", "хватит", "отбой", "спасибо", "всё", "все", "замолч", "стоп")
            # Accept if the quoted words were transcribed, OR the quote is a clear farewell and the user
            # really said something in the last 6 s (the transcriber mangled «Всё, пока» into «Чопан»);
            # hallucinated farewells come on empty/echo transcripts, which do not count as user text.
            said_recently = time.monotonic() - self.last_user_text_at < 6.0
            plausible = any(w in quote.lower().replace("ё", "е") for w in farewell_words) and said_recently
            if len(quote) < 3 or not (_quote_in(quote, heard) or plausible):
                log.warning("end_conversation refused: quote=%r, recent=%r", quote, heard)
                return {"ok": False, "error": "пользователь не прощался — продолжай разговор", "_no_response": True}
            log.info("agent ends the conversation: %r", quote)
            asyncio.get_running_loop().create_task(self._end_after_playback())
            return {"ok": True, "_no_response": True}
        if name in ("home_assistant", "climate_control", "device_control"):
            if name == "home_assistant":
                res = await self._ha_command(args.get("command") or "")
            elif name == "climate_control":
                res = await self._climate(args)
            else:
                res = await self._device(args)
            if self.b.end_after_action and isinstance(res, dict) and res.get("ok") and res.get("response_type", "action_done") == "action_done":
                self.pending_end = f"action done ({name})"
                self.pending_end_at = time.monotonic()
                self.pending_end_audio_at = 0.0
                asyncio.get_running_loop().create_task(self._end_after_confirmation())
            return res
        return {"error": f"unknown tool {name}", "_no_response": True}

    async def _ha_command(self, command: str) -> dict:
        """Hand the phrase to a HA conversation agent (built-in intents or any other agent)."""
        if not command:
            return {"error": "empty command"}
        areas = [a.name for a in ar.async_get(self.b.hass).async_list_areas() if a.name]
        names, _dups = exposed_names(self.b.hass)
        normalized = normalize_names(command, names)
        # area normalization must not rewrite words inside an already exact entity name
        # («Охлаждение спальни» → «Охлаждение Спальня» broke the match): mask names first
        masks: dict[str, str] = {}
        for i, n in enumerate(sorted(set(names), key=len, reverse=True)):
            if n and n in normalized:
                key = f"\x00{i}\x00"
                masks[key] = n
                normalized = normalized.replace(n, key)
        normalized = normalize_areas(normalized, areas)
        for key, n in masks.items():
            normalized = normalized.replace(key, n)
        if normalized != command:
            log.info("area names normalized: %r → %r", command, normalized)
            command = normalized
        # Pre-router: «включи/выключи <точное имя> [в <зона>]» for one unique exposed entity goes
        # straight to the service. HA's sentence matcher otherwise routes e.g. «включи Вентилятор в
        # Гостиная» to its fan-domain rule, whose Russian response template is broken.
        direct = await self._direct_on_off(command)
        if direct is not None:
            return direct
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

    _ONOFF_RE = re.compile(r"^\s*(включи|включить|выключи|выключить)\s+(.+?)(?:\s+(?:в|во|на)\s+(.+?))?\s*[.!]?\s*$", re.I)
    _GENERIC = {"свет", "лампа", "лампы", "музыка", "музыку", "всё", "все", "всe"}

    async def _press_by_phrase(self, command: str) -> dict | None:
        """«выключи комп» == alias «Выключить комп» of a button/script → press/run it. Buttons and
        scripts are named by what they do, so the whole phrase (verb in the infinitive) is the key."""
        hass = self.b.hass
        phrase = command.replace("ё", "е").strip().rstrip(".!").lower()
        phrase = re.sub(r"^(включи|включить)\b", "включить", phrase)
        phrase = re.sub(r"^(выключи|выключить)\b", "выключить", phrase)
        ent_reg = er.async_get(hass)
        for st in hass.states.async_all():
            dom = st.entity_id.split(".")[0]
            if dom not in ("button", "script") or not async_should_expose(hass, "conversation", st.entity_id):
                continue
            ent = ent_reg.async_get(st.entity_id)
            names = [st.name.lower().replace("ё", "е")] + [a.lower().replace("ё", "е") for a in ((ent.aliases if ent else None) or ()) if isinstance(a, str)]
            if phrase in names:
                if st.state == "unavailable":
                    return {"ok": False, "error": f"{st.name} сейчас недоступно (unavailable)", "targets": [st.name]}
                svc = ("button", "press") if dom == "button" else ("script", "turn_on")
                try:
                    await hass.services.async_call(svc[0], svc[1], {"entity_id": st.entity_id}, blocking=True)
                except Exception as e:
                    return {"ok": False, "error": str(e)[:200]}
                res = {"ok": True, "response_type": "action_done", "speech": f"{st.name}: {svc[1]}", "targets": [st.name]}
                log.info("press-by-phrase %r → %s", command, res)
                return res
        return None

    async def _direct_on_off(self, command: str) -> dict | None:
        pressed = await self._press_by_phrase(command)
        if pressed is not None:
            return pressed
        m = self._ONOFF_RE.match(command.replace("ё", "е"))
        if not m:
            log.debug("direct: no verb match for %r", command)
            return None
        verb, name, area = m.group(1).lower(), m.group(2).strip(), (m.group(3) or "").strip()
        if name.lower() in self._GENERIC or len(name) < 4:
            return None
        hass = self.b.hass
        ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
        areas = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
        want = name.lower()
        cands = []
        for st in hass.states.async_all():
            dom = st.entity_id.split(".")[0]
            if dom in ("sensor", "binary_sensor", "climate", "scene", "automation", "script", "button", "event", "update", "media_player"):
                continue
            if not async_should_expose(hass, "conversation", st.entity_id):
                continue
            ent = ent_reg.async_get(st.entity_id)
            names = [st.name.lower().replace("ё", "е")] + [a.lower().replace("ё", "е") for a in ((ent.aliases if ent else None) or ()) if isinstance(a, str)]
            if want not in names:
                continue
            aid = (ent.area_id if ent else None) or (dev_reg.async_get(ent.device_id).area_id if ent and ent.device_id and dev_reg.async_get(ent.device_id) else None)
            if area and areas.get(aid, "").lower() != area.lower():
                continue
            cands.append(st)
        if len(cands) != 1:
            log.info("direct: %d candidates for %r (area=%r) — leaving it to HA", len(cands), name, area)
            return None   # let HA's own matcher handle it (and report duplicates / misses)
        st = cands[0]
        if st.state == "unavailable":
            return {"ok": False, "error": f"{st.name} сейчас недоступно (unavailable)", "targets": [st.name]}
        service = "turn_on" if verb.startswith("вкл") else "turn_off"
        try:
            await hass.services.async_call("homeassistant", service, {"entity_id": st.entity_id}, blocking=True)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        await asyncio.sleep(0.8)
        now = hass.states.get(st.entity_id)
        res = {"ok": True, "response_type": "action_done", "speech": f"{st.name}: {service}", "targets": [st.name],
               "state_now": now.state if now else None}
        log.info("direct %s → %s", command, res)
        return res

    async def _device(self, args: dict) -> dict:
        """Direct on/off by exact exposed name — bypasses sentence matching (which e.g. routes
        «Вентилятор в Гостиная» to the fan-domain rule and dies in a broken response template)."""
        hass = self.b.hass
        want = (args.get("name") or "").strip().lower().replace("ё", "е")
        action = args.get("action")
        ent_reg = er.async_get(hass)
        cands = []
        for st in hass.states.async_all():
            dom = st.entity_id.split(".")[0]
            if dom in ("sensor", "binary_sensor", "climate", "scene", "automation", "event", "update"):
                continue
            if not async_should_expose(hass, "conversation", st.entity_id):
                continue
            ent = ent_reg.async_get(st.entity_id)
            names = [st.name.lower().replace("ё", "е")] + [a.lower().replace("ё", "е") for a in ((ent.aliases if ent else None) or ()) if isinstance(a, str)]
            if want in names:
                cands.append(st)
        if not cands:
            return {"ok": False, "error": f"устройство «{args.get('name')}» не найдено в списке — назови точное имя"}
        if len(cands) > 1:
            areas = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
            dev_reg = dr.async_get(hass)
            def area_of(st):
                ent = ent_reg.async_get(st.entity_id)
                aid = (ent.area_id if ent else None) or (dev_reg.async_get(ent.device_id).area_id if ent and ent.device_id and dev_reg.async_get(ent.device_id) else None)
                return areas.get(aid, "без зоны")
            return {"ok": False, "error": "несколько устройств с таким именем: " + ", ".join(f"{c.name} ({area_of(c)})" for c in cands)
                    + " — уточни у пользователя зону и используй home_assistant с зоной"}
        st = cands[0]
        if st.state == "unavailable":
            return {"ok": False, "error": f"{st.name} сейчас недоступно (unavailable)"}
        dom = st.entity_id.split(".")[0]
        if dom == "button":
            domain_svc, service = "button", "press"
        elif dom == "script":
            domain_svc, service = "script", "turn_on"
        elif action in ("turn_on", "turn_off", "toggle"):
            domain_svc, service = "homeassistant", action
        else:
            return {"ok": False, "error": f"неизвестное действие {action}"}
        try:
            await hass.services.async_call(domain_svc, service, {"entity_id": st.entity_id}, blocking=True)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        await asyncio.sleep(1.0)
        now = hass.states.get(st.entity_id)
        res = {"ok": True, "entity": st.name, "action": service, "state_now": now.state if now else None}
        log.info("device_control %s → %s", args, res)
        return res

    async def _climate(self, args: dict) -> dict:
        """Direct climate control: HA's built-in intents can set a temperature but cannot switch a
        climate entity on/off, so «включи кондиционер» needs a service call."""
        hass = self.b.hass
        want = (args.get("name") or "").strip().lower().replace("ё", "е")
        cands = [st for st in hass.states.async_all("climate") if async_should_expose(hass, "conversation", st.entity_id)]
        match = [st for st in cands if st.name.lower().replace("ё", "е") == want] or \
                [st for st in cands if want and (want in st.name.lower() or st.name.lower()[:5] in want)] or \
                (cands if len(cands) == 1 else [])
        if not match:
            return {"ok": False, "error": f"климат-устройство «{args.get('name')}» не найдено; есть: {[c.name for c in cands]}"}
        st = match[0]
        modes = st.attributes.get("hvac_modes") or ["cool", "heat", "off"]
        action = args.get("action")
        try:
            if action == "turn_off":
                await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": st.entity_id, "hvac_mode": "off"}, blocking=True)
                result = {"ok": True, "entity": st.name, "hvac_mode": "off"}
            elif action in ("turn_on", "set_mode"):
                mode = args.get("mode") or "cool"
                if mode not in modes:
                    return {"ok": False, "error": f"режим {mode} недоступен, доступны: {modes}"}
                await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": st.entity_id, "hvac_mode": mode}, blocking=True)
                result = {"ok": True, "entity": st.name, "hvac_mode": mode}
            elif action == "set_temperature":
                t = args.get("temperature")
                if t is None:
                    return {"ok": False, "error": "не указана температура"}
                lo, hi = st.attributes.get("min_temp", 16), st.attributes.get("max_temp", 30)
                t = max(lo, min(hi, float(t)))
                data = {"entity_id": st.entity_id, "temperature": t}
                if st.state == "off":
                    data["hvac_mode"] = "cool" if "cool" in modes else modes[0]
                await hass.services.async_call("climate", "set_temperature", data, blocking=True)
                result = {"ok": True, "entity": st.name, "temperature": t, "hvac_mode": data.get("hvac_mode", st.state)}
            else:
                return {"ok": False, "error": f"неизвестное действие {action}"}
        except Exception as e:
            log.warning("climate call failed: %s", e)
            return {"ok": False, "error": str(e)[:200]}
        await asyncio.sleep(0.5)
        if action == "set_temperature":
            # The house has automations that react to the AC switching on by applying their own
            # preset (climate off→cool → downstairs toggle → set_temperature 27). Give them 2 s,
            # then re-apply the temperature the user actually asked for.
            await asyncio.sleep(2.0)
            now = hass.states.get(st.entity_id)
            if now and now.attributes.get("temperature") not in (None, result["temperature"]):
                log.info("temperature overridden to %s by an automation — re-applying %s", now.attributes.get("temperature"), result["temperature"])
                await hass.services.async_call("climate", "set_temperature", {"entity_id": st.entity_id, "temperature": result["temperature"]}, blocking=True)
                await asyncio.sleep(0.5)
        now = hass.states.get(st.entity_id)
        result["state_now"] = {"hvac_mode": now.state, "target": now.attributes.get("temperature"),
                               "current": now.attributes.get("current_temperature")} if now else None
        log.info("climate %s → %s", args, result)
        return result

    async def _end_after_confirmation(self):
        """Command executed: let the model say «Готово», wait for the device to finish playing it,
        then end — unless the user started talking again meanwhile."""
        reason = self.pending_end
        for _ in range(80):            # up to 8 s for the confirmation audio to start
            await asyncio.sleep(0.1)
            if self.pending_end is None or self.ending:
                return
            if self.stream is not None and self.stream.closed:
                break
        await self.b.dev.wait_playback_done(15)
        await asyncio.sleep(0.3)
        # Keep the session only if the user really said something after the command (a non-empty
        # transcript) or is talking right now; a VAD blip from the echo of «Готово» does not count.
        # The command's own transcript arrives after the tool call — only text that came in after the
        # confirmation started playing means the user went on talking.
        baseline = (self.pending_end_audio_at or self.pending_end_at) + 0.3
        if self.pending_end is None or self.ending or self.oai.in_speech or self.last_user_text_at > baseline:
            log.info("end-after-action skipped: user continued")
            self.pending_end = None
            return
        await self.end(reason or "action done")

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
        persona = (o.get(OPT_INSTRUCTIONS) or "").strip()
        if not persona or "инструмент" in persona.lower():
            persona = DEFAULT_PERSONA   # empty, or a stale copy of an old full prompt saved by the form
        self.instructions = persona + "\n\n" + TOOL_RULES
        self.end_after_action = bool(o.get(OPT_END_AFTER_ACTION, DEFAULT_END_AFTER_ACTION))
        self.agc = bool(o.get(OPT_AGC, DEFAULT_AGC))
        self.agc_min, self.agc_max = 1.0, 12.0
        self.agc_level = int(o.get(OPT_AGC_LEVEL, DEFAULT_AGC_LEVEL))
        self.ns_level = int(o.get(OPT_NS_LEVEL, DEFAULT_NS_LEVEL))
        self.record = bool(o.get(OPT_RECORD, DEFAULT_RECORD))
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
        self.start_mute = float(o.get(OPT_START_MUTE, DEFAULT_START_MUTE))
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
