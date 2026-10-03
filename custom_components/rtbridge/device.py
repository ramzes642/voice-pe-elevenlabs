"""Voice PE over the ESPHome native API: we play the role of Home Assistant's voice assistant.

Facts (from esphome/components/voice_assistant, 2026.5):
  * After `voice_assistant.start` the device sends VoiceAssistantRequest(start) and, once we
    answer with port=0, streams mic audio as VoiceAssistantAudio (16 kHz s16le mono; `data`
    = channel 0 (AGC), `data2` = channel 1 (raw)) for as long as it stays in
    STREAMING_MICROPHONE — i.e. until we send STT_VAD_END / STT_END / RUN_END or an error.
  * Response audio: the Voice PE's voice_assistant is bound to a media_player (not a raw
    speaker), so VoiceAssistantAudio downstream is unavailable; instead we command the media
    player to play an HTTP URL as an announcement. That runs on the announcement mixer input,
    independent of the microphone state → full duplex, with the XMOS AEC cancelling it.
  * NEVER send TTS_END while a session is live: it flips the device to STREAMING_RESPONSE
    and the mic stops being forwarded. RUN_END ends the session cleanly (→ IDLE, on_end).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from aioesphomeapi import (
    APIClient,
    MediaPlayerCommand,
    MediaPlayerEntityState,
    MediaPlayerInfo,
    MediaPlayerState,
    ReconnectLogic,
    VoiceAssistantAudioSettings,
    VoiceAssistantEventType as EV,
)

log = logging.getLogger(__name__)

AudioCb = Callable[[bytes, bytes | None], Awaitable[None]]
StartCb = Callable[[str, str | None], Awaitable[None]]      # (conversation_id, wake_word)
StopCb = Callable[[bool], Awaitable[None]]                  # (aborted_by_device)


def psk_from_yaml(path: str | Path) -> str | None:
    """api: encryption: key: "…" from an ESPHome YAML (the device's own config)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    m = re.search(r'^\s+key:\s*"([A-Za-z0-9+/=]{40,})"', text, re.M)
    return m.group(1) if m else None


class VoicePE:
    def __init__(self, host: str, psk: str, *, on_start: StartCb, on_audio: AudioCb, on_stop: StopCb,
                 port: int = 6053):
        self.client = APIClient(host, port, password=None, noise_psk=psk, client_info="rtbridge-ha")
        self.on_start, self.on_audio, self.on_stop = on_start, on_audio, on_stop
        self.media_key: int | None = None
        self.media_state: MediaPlayerState | None = None
        self.connected = asyncio.Event()
        self._unsub_va = None
        self._reconnect: ReconnectLogic | None = None
        self._player_idle = asyncio.Event()
        self._player_idle.set()
        self._announcing_since: float | None = None

    # ---- lifecycle --------------------------------------------------------------------
    async def start(self):
        self._reconnect = ReconnectLogic(client=self.client, on_connect=self._on_connect,
                                         on_disconnect=self._on_disconnect, zeroconf_instance=None,
                                         name="voice-pe", on_connect_error=self._on_connect_error)
        await self._reconnect.start()

    async def stop(self):
        if self._reconnect:
            await self._reconnect.stop()
        await self.client.disconnect()

    async def _on_connect_error(self, err: Exception):
        log.warning("connect error: %s", err)

    async def _on_connect(self):
        info = await self.client.device_info()
        log.info("connected to %s (%s, esphome %s, va flags=%s)", info.name, info.friendly_name,
                 info.esphome_version, info.voice_assistant_feature_flags_compat(self.client.api_version))
        entities, _services = await self.client.list_entities_services()
        self.media_key = None
        for e in entities:
            if isinstance(e, MediaPlayerInfo):
                self.media_key = e.key
                log.info("media_player entity: %s key=%s", e.object_id, e.key)
        if self.media_key is None:
            log.error("device has no media_player entity — cannot play responses")
        self.client.subscribe_states(self._on_state)
        self._unsub_va = self.client.subscribe_voice_assistant(
            handle_start=self._handle_start, handle_stop=self._handle_stop, handle_audio=self._handle_audio)
        self.connected.set()
        log.info("subscribed as the device's voice assistant")

    async def _on_disconnect(self, expected: bool):
        self.connected.clear()
        log.warning("disconnected (expected=%s)", expected)
        await self.on_stop(True)

    def _on_state(self, state):
        if isinstance(state, MediaPlayerEntityState) and state.key == self.media_key:
            self.media_state = state.state
            log.debug("media_player state: %s", state.state)
            if state.state in (MediaPlayerState.IDLE, MediaPlayerState.NONE, MediaPlayerState.PAUSED):
                self._player_idle.set()
                self._announcing_since = None
            else:
                self._player_idle.clear()
                if self._announcing_since is None:
                    self._announcing_since = asyncio.get_running_loop().time()

    # ---- voice assistant server side --------------------------------------------------
    async def _handle_start(self, conversation_id: str, flags: int, settings: VoiceAssistantAudioSettings,
                            wake_word: str | None) -> int | None:
        log.info("device: start (conversation=%r flags=%s wake_word=%r)", conversation_id, flags, wake_word)
        asyncio.get_running_loop().create_task(self.on_start(conversation_id, wake_word))
        return 0  # 0 = send audio over the API connection

    async def _handle_stop(self, aborted: bool):
        log.info("device: stop (aborted=%s)", aborted)
        await self.on_stop(True)

    async def _handle_audio(self, data: bytes, data2: bytes | None):
        await self.on_audio(data, data2)

    # ---- commands to the device ------------------------------------------------------
    def event(self, ev: EV, data: dict[str, str] | None = None):
        if self.connected.is_set():
            self.client.send_voice_assistant_event(ev, data)

    def session_begin(self):
        """Lights/phase on the device: pipeline running, waiting for the user."""
        self.event(EV.VOICE_ASSISTANT_RUN_START)
        self.event(EV.VOICE_ASSISTANT_STT_START)

    def user_speaking(self):
        self.event(EV.VOICE_ASSISTANT_STT_VAD_START)

    def agent_speaking(self, text: str = "…"):
        # TTS_START with text: "replying" LEDs + the device logs the text. Does not change state
        # in media_player mode. (Also arms the "stop" wake word after ~1 s of playback.)
        self.event(EV.VOICE_ASSISTANT_TTS_START, {"text": text[:400] or "…"})

    def session_end(self):
        self.event(EV.VOICE_ASSISTANT_RUN_END)

    def error(self, code: str, message: str):
        self.event(EV.VOICE_ASSISTANT_ERROR, {"code": code, "message": message})

    def play_url(self, url: str):
        if self.media_key is not None and self.connected.is_set():
            self._player_idle.clear()
            self.client.media_player_command(self.media_key, media_url=url, announcement=True)

    def stop_playback(self, min_age: float = 1.5) -> bool:
        """STOP the announcement — but only if it has been playing for at least `min_age` s.
        Stopping a just-started announcement races the decoder task start-up and has crashed the
        device (double free in pthread TLS cleanup). The HTTP stream is paced to real time, so
        for a young announcement simply closing the stream ends playback within ~1 s."""
        if self.media_key is None or not self.connected.is_set():
            return False
        if self._announcing_since is None:
            return False
        age = asyncio.get_running_loop().time() - self._announcing_since
        if age < min_age:
            log.info("not stopping a %.1fs-old announcement (stream closed instead)", age)
            return False
        self.client.media_player_command(self.media_key, command=MediaPlayerCommand.STOP, announcement=True)
        return True

    async def wait_playback_done(self, timeout: float = 30.0):
        try:
            await asyncio.wait_for(self._player_idle.wait(), timeout)
        except asyncio.TimeoutError:
            log.warning("playback did not finish within %.0fs", timeout)
