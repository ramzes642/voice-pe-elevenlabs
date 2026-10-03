from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (CONF_API_KEY, CONF_ESPHOME_ENTRY, CONF_HOST, CONF_NOISE_PSK, DEFAULT_COMMAND_AGENT,
                    DEFAULT_IDLE_TIMEOUT, DEFAULT_INSTRUCTIONS, DEFAULT_LANGUAGE, DEFAULT_MAX_SESSION,
                    DEFAULT_MIC_GAIN, DEFAULT_MODEL, DEFAULT_VAD_EAGERNESS, DEFAULT_VOICE, DOMAIN,
                    OPT_AUDIO_BASE_URL, OPT_COMMAND_AGENT, OPT_GREETING, OPT_HA_TOOL, OPT_IDLE_TIMEOUT,
                    OPT_INSTRUCTIONS, OPT_LANGUAGE, OPT_MAX_SESSION, OPT_MIC_GAIN, OPT_MODEL, OPT_VAD_EAGERNESS,
                    OPT_VOICE)

log = logging.getLogger(__name__)


async def _check_openai_key(hass, key: str) -> str | None:
    try:
        r = await async_get_clientsession(hass).get("https://api.openai.com/v1/models",
                                                    headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if r.status == 401:
            return "invalid_auth"
        if r.status >= 400:
            return "cannot_connect"
    except Exception:
        return "cannot_connect"
    return None


class RtBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        esphome_entries = [e for e in self.hass.config_entries.async_entries("esphome") if e.data.get("host")]
        if user_input is not None:
            entry = next((e for e in esphome_entries if e.entry_id == user_input[CONF_ESPHOME_ENTRY]), None)
            if entry is None:
                errors["base"] = "no_device"
            elif err := await _check_openai_key(self.hass, user_input[CONF_API_KEY]):
                errors["base"] = err
            else:
                await self.async_set_unique_id(entry.entry_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Realtime voice: {entry.title}",
                    data={CONF_ESPHOME_ENTRY: entry.entry_id, CONF_HOST: entry.data["host"],
                          CONF_NOISE_PSK: entry.data.get("noise_psk", ""), CONF_API_KEY: user_input[CONF_API_KEY]},
                    options={OPT_MODEL: DEFAULT_MODEL, OPT_VOICE: DEFAULT_VOICE, OPT_COMMAND_AGENT: DEFAULT_COMMAND_AGENT,
                             OPT_LANGUAGE: DEFAULT_LANGUAGE, OPT_GREETING: True, OPT_HA_TOOL: True,
                             OPT_IDLE_TIMEOUT: DEFAULT_IDLE_TIMEOUT, OPT_MAX_SESSION: DEFAULT_MAX_SESSION,
                             OPT_MIC_GAIN: DEFAULT_MIC_GAIN, OPT_VAD_EAGERNESS: DEFAULT_VAD_EAGERNESS})
        options = [selector.SelectOptionDict(value=e.entry_id, label=e.title) for e in esphome_entries]
        schema = vol.Schema({
            vol.Required(CONF_ESPHOME_ENTRY): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Required(CONF_API_KEY): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RtBridgeOptionsFlow()


class RtBridgeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            vol.Required(OPT_MODEL, default=o.get(OPT_MODEL, DEFAULT_MODEL)): str,
            vol.Required(OPT_VOICE, default=o.get(OPT_VOICE, DEFAULT_VOICE)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"],
                                              custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Optional(OPT_INSTRUCTIONS, default=o.get(OPT_INSTRUCTIONS) or DEFAULT_INSTRUCTIONS): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)),
            vol.Required(OPT_HA_TOOL, default=o.get(OPT_HA_TOOL, True)): bool,
            vol.Required(OPT_COMMAND_AGENT, default=o.get(OPT_COMMAND_AGENT, DEFAULT_COMMAND_AGENT)): selector.ConversationAgentSelector(
                selector.ConversationAgentSelectorConfig(language=o.get(OPT_LANGUAGE, DEFAULT_LANGUAGE))),
            vol.Required(OPT_LANGUAGE, default=o.get(OPT_LANGUAGE, DEFAULT_LANGUAGE)): str,
            vol.Required(OPT_GREETING, default=o.get(OPT_GREETING, True)): bool,
            vol.Required(OPT_VAD_EAGERNESS, default=o.get(OPT_VAD_EAGERNESS, DEFAULT_VAD_EAGERNESS)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["auto", "low", "medium", "high"], mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Required(OPT_IDLE_TIMEOUT, default=o.get(OPT_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)): vol.All(int, vol.Range(5, 600)),
            vol.Required(OPT_MAX_SESSION, default=o.get(OPT_MAX_SESSION, DEFAULT_MAX_SESSION)): vol.All(int, vol.Range(30, 7200)),
            vol.Required(OPT_MIC_GAIN, default=o.get(OPT_MIC_GAIN, DEFAULT_MIC_GAIN)): vol.All(vol.Coerce(float), vol.Range(1, 64)),
            vol.Optional(OPT_AUDIO_BASE_URL, description={"suggested_value": o.get(OPT_AUDIO_BASE_URL, "")}): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
