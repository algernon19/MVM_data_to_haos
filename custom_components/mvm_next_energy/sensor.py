"""Sensor platform for MVM Next Energy Import."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .importer import MvmImportCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the MVM Next Energy Import sensor."""
    coordinator: MvmImportCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MvmNextImportSensor(entry, coordinator)])


class MvmNextImportSensor(SensorEntity):
    """Shows how far the imported MVM history reaches."""

    _attr_name = "MVM Next Import"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: MvmImportCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_import"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._coordinator.state_last_quarter

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self._coordinator.attributes
