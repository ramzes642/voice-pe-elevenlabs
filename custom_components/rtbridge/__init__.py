"""Realtime Voice Bridge: Voice PE ⇄ OpenAI Realtime, with Home Assistant as a tool."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .bridge import RtBridge
from .const import DOMAIN
from .http import StreamView

log = logging.getLogger(__name__)
VIEW_KEY = f"{DOMAIN}_view"
# Conversation transcripts / tool calls are the useful trace of this integration: log them at INFO
# even when HA's default level is WARNING (override with logger: custom_components.rtbridge: warning).
logging.getLogger("custom_components.rtbridge").setLevel(logging.INFO)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not hass.data.get(VIEW_KEY):
        hass.http.register_view(StreamView())
        hass.data[VIEW_KEY] = True
    bridge = RtBridge(hass, entry)
    await bridge.start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = bridge
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge: RtBridge | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if bridge:
        await bridge.stop()
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
