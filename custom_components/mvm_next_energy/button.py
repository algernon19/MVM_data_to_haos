"""Button platform for MVM Next Energy Import."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .importer import MvmImportCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the MVM Next Energy Import button."""
    coordinator: MvmImportCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MvmNextImportButton(entry, coordinator)])


class MvmNextImportButton(ButtonEntity):
    """Triggers a scan of the import directory and pushes updated statistics."""

    _attr_name = "MVM CSV importálása"
    _attr_icon = "mdi:file-import"

    def __init__(self, entry: ConfigEntry, coordinator: MvmImportCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_import_button"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        await self._coordinator.async_import(None)
