"""Sensor platform for MVM Next Energy Import."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .importer import MvmImportCoordinator


@dataclass(frozen=True, kw_only=True)
class MvmSummarySensor:
    """Description of a sensor backed by coordinator.year_summary / current_d."""

    key: str
    name: str
    icon: str
    unit: str | None = None
    currency_unit: bool = False
    source: str = "year"  # "year" -> year_summary, "current" -> current_d


SUMMARY_SENSORS: tuple[MvmSummarySensor, ...] = (
    MvmSummarySensor(
        key="year_consumption",
        name="MVM Next Éves fogyasztás",
        icon="mdi:counter",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="year_allowance",
        name="MVM Next Idei kedvezményes keret",
        icon="mdi:gauge-low",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="allowance_remaining",
        name="MVM Next Hátralévő kedvezményes keret",
        icon="mdi:gauge",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="allowance_used_pct",
        name="MVM Next Kedvezményes keret kihasználtság",
        icon="mdi:percent",
        unit="%",
    ),
    MvmSummarySensor(
        key="year_cost",
        name="MVM Next Éves költség",
        icon="mdi:cash-multiple",
        currency_unit=True,
    ),
    MvmSummarySensor(
        key="price_tier",
        name="MVM Next Aktuális ársáv",
        icon="mdi:cash",
    ),
    MvmSummarySensor(
        key="tier_crossover_estimate",
        name="MVM Next Becsült sávváltás",
        icon="mdi:calendar-alert",
    ),
    MvmSummarySensor(
        key="year_cost_d",
        name="MVM Next D tarifa idei költség",
        icon="mdi:cash-clock",
        currency_unit=True,
    ),
    MvmSummarySensor(
        key="year_cost_a1_vs_d",
        name="MVM Next A1 és D különbség",
        icon="mdi:scale-balance",
        currency_unit=True,
    ),
    MvmSummarySensor(
        key="gross_huf_kwh",
        name="MVM Next D tarifa aktuális ár",
        icon="mdi:cash-fast",
        unit="HUF/kWh",
        source="current",
    ),
    MvmSummarySensor(
        key="raw_huf_kwh",
        name="MVM Next D tarifa HUPX nyers ár",
        icon="mdi:chart-line",
        unit="HUF/kWh",
        source="current",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the MVM Next Energy Import sensors."""
    coordinator: MvmImportCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [MvmNextImportSensor(entry, coordinator)]
    entities += [
        MvmNextSummarySensor(entry, coordinator, desc) for desc in SUMMARY_SENSORS
    ]
    async_add_entities(entities)


class _MvmBaseSensor(SensorEntity):
    """Shared wiring: refresh on the coordinator's update signal."""

    _attr_should_poll = False

    def __init__(self, coordinator: MvmImportCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class MvmNextImportSensor(_MvmBaseSensor):
    """Shows how far the imported MVM history reaches."""

    _attr_name = "MVM Next Import"
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, entry: ConfigEntry, coordinator: MvmImportCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_import"

    @property
    def native_value(self) -> str | None:
        return self._coordinator.state_last_quarter

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self._coordinator.attributes


class MvmNextSummarySensor(_MvmBaseSensor):
    """One figure from the current-year allowance summary."""

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: MvmImportCoordinator,
        description: MvmSummarySensor,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        if description.source == "current":
            self._attr_unique_id = f"{entry.entry_id}_current_{description.key}"

    @property
    def _data(self) -> dict:
        if self._description.source == "current":
            return self._coordinator.current_d
        return self._coordinator.year_summary

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._description.currency_unit:
            return self.hass.config.currency
        return self._description.unit

    @property
    def native_value(self) -> object:
        value = self._data.get(self._description.key)
        if self._description.key == "price_tier":
            return {"kedvezmenyes": "kedvezményes", "piaci": "piaci"}.get(
                value, value
            )
        if self._description.key == "tier_crossover_estimate":
            return {
                "atlepve": "átlépve",
                "ismeretlen": "ismeretlen",
            }.get(value, value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        summary = self._coordinator.year_summary
        if self._description.source == "current":
            data = self._coordinator.current_d
            return {
                "slot_start": data.get("slot_start"),
                "hupx_eur_mwh": data.get("hupx_eur_mwh"),
                "eur_huf": data.get("eur_huf"),
                "forecast": data.get("forecast"),
            }
        attrs: dict[str, object] = {
            "year": summary.get("year"),
            "data_through": summary.get("data_through"),
        }
        if self._description.key == "year_cost_a1_vs_d":
            attrs["monthly_comparison"] = summary.get("monthly_comparison")
            attrs["d_slots_priced"] = summary.get("d_slots_priced")
            attrs["d_slots_total"] = summary.get("d_slots_total")
            attrs["d_eur_huf_source"] = summary.get("d_eur_huf_source")
            attrs["d_error"] = summary.get("d_error")
        return attrs
