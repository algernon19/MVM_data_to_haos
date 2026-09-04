"""The MVM Next Energy Import integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATTR_FILE,
    ATTR_FILE_PATH,
    DOMAIN,
    SERVICE_IMPORT,
    SERVICE_UPLOAD,
)
from .importer import MvmImportCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]

SERVICE_IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FILE_PATH): cv.string,
    }
)

SERVICE_UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_FILE): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MVM Next Energy Import from a config entry."""
    coordinator = MvmImportCoordinator(hass, entry)
    await coordinator.async_load_cache()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _refresh_current(_now=None) -> None:
        await coordinator.async_refresh_current()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.async_create_task(_refresh_current())
    entry.async_on_unload(
        async_track_time_interval(hass, _refresh_current, timedelta(minutes=15))
    )

    async def _handle_import(call: ServiceCall) -> None:
        await coordinator.async_import(call.data.get(ATTR_FILE_PATH))

    async def _handle_upload(call: ServiceCall) -> None:
        await coordinator.async_upload(call.data[ATTR_FILE])

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT):
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT, _handle_import, schema=SERVICE_IMPORT_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPLOAD):
        hass.services.async_register(
            DOMAIN, SERVICE_UPLOAD, _handle_upload, schema=SERVICE_UPLOAD_SCHEMA
        )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recompute statistics when the pricing options change."""
    coordinator: MvmImportCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_import(None)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT)
            hass.services.async_remove(DOMAIN, SERVICE_UPLOAD)
    return unload_ok
