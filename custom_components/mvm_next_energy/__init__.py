"""The MVM Next Energy Import integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import ATTR_FILE_PATH, CONF_IMPORT_DIR, DEFAULT_IMPORT_DIR, DOMAIN, SERVICE_IMPORT
from .importer import MvmImportCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]

SERVICE_IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FILE_PATH): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MVM Next Energy Import from a config entry."""
    import_dir = entry.data.get(CONF_IMPORT_DIR, DEFAULT_IMPORT_DIR)
    coordinator = MvmImportCoordinator(hass, entry, import_dir)
    await coordinator.async_load_cache()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_import(call: ServiceCall) -> None:
        await coordinator.async_import(call.data.get(ATTR_FILE_PATH))

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT):
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT, _handle_import, schema=SERVICE_IMPORT_SCHEMA
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT)
    return unload_ok
