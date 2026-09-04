"""Config flow for the MVM Next Energy Import integration."""
from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    DateSelector,
    FileSelector,
    FileSelectorConfig,
)

from .const import (
    ATTR_FILE,
    CONF_ANNUAL_THRESHOLD,
    CONF_COST_ENABLED,
    CONF_IMPORT_DIR,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    CONF_START_DATE,
    DEFAULT_ANNUAL_THRESHOLD,
    DEFAULT_COST_ENABLED,
    DEFAULT_PRICE_HIGH,
    DEFAULT_PRICE_LOW,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class MvmNextEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MVM Next Energy Import."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        default_dir = self.hass.config.path("mvm_next")

        if user_input is not None:
            import_dir = user_input[CONF_IMPORT_DIR].strip() or default_dir

            if not await self.hass.async_add_executor_job(self._ensure_dir, import_dir):
                errors["base"] = "invalid_dir"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="MVM Next Energy Import",
                    data={CONF_IMPORT_DIR: import_dir},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_DIR, default=default_dir): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "MvmNextEnergyOptionsFlow":
        return MvmNextEnergyOptionsFlow()

    @staticmethod
    def _ensure_dir(path: str) -> bool:
        """Create the import directory if needed. Returns False on failure."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return False
        return os.path.isdir(path)


class MvmNextEnergyOptionsFlow(config_entries.OptionsFlow):
    """Provide a browser file-upload UI via the integration's 'Configure' button."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["upload", "folder", "pricing"]
        )

    async def async_step_folder(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        default_dir = current.get(CONF_IMPORT_DIR) or self.hass.config.path("mvm_next")

        if user_input is not None:
            import_dir = (
                user_input[CONF_IMPORT_DIR].strip()
                or self.hass.config.path("mvm_next")
            )
            if not await self.hass.async_add_executor_job(
                MvmNextEnergyConfigFlow._ensure_dir, import_dir
            ):
                errors["base"] = "invalid_dir"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_IMPORT_DIR: import_dir,
                    },
                )

        schema = vol.Schema(
            {vol.Required(CONF_IMPORT_DIR, default=default_dir): str}
        )
        return self.async_show_form(
            step_id="folder", data_schema=schema, errors=errors
        )

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            new_options = {
                **self.config_entry.options,
                CONF_COST_ENABLED: user_input[CONF_COST_ENABLED],
                CONF_PRICE_LOW: user_input[CONF_PRICE_LOW],
                CONF_PRICE_HIGH: user_input[CONF_PRICE_HIGH],
                CONF_ANNUAL_THRESHOLD: user_input[CONF_ANNUAL_THRESHOLD],
                CONF_START_DATE: user_input.get(CONF_START_DATE, ""),
            }
            # The entry update listener re-pushes both statistics with the new
            # settings (no CSV re-parse needed).
            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_COST_ENABLED,
                    default=current.get(CONF_COST_ENABLED, DEFAULT_COST_ENABLED),
                ): bool,
                vol.Required(
                    CONF_PRICE_LOW,
                    default=current.get(CONF_PRICE_LOW, DEFAULT_PRICE_LOW),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_PRICE_HIGH,
                    default=current.get(CONF_PRICE_HIGH, DEFAULT_PRICE_HIGH),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_ANNUAL_THRESHOLD,
                    default=current.get(
                        CONF_ANNUAL_THRESHOLD, DEFAULT_ANNUAL_THRESHOLD
                    ),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_START_DATE,
                    description={
                        "suggested_value": current.get(CONF_START_DATE) or None
                    },
                ): DateSelector(),
            }
        )
        return self.async_show_form(step_id="pricing", data_schema=schema)

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        self._upload_error = ""

        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(
                self.config_entry.entry_id
            )
            if coordinator is None:
                errors["base"] = "not_loaded"
            else:
                try:
                    await coordinator.async_upload(user_input[ATTR_FILE])
                except HomeAssistantError as err:
                    _LOGGER.exception("MVM Next feltöltés hiba")
                    errors["base"] = "upload_failed"
                    self._upload_error = str(err)
                except Exception as err:  # noqa: BLE001 - surface any parse/IO failure
                    _LOGGER.exception("MVM Next feltöltés hiba")
                    errors["base"] = "upload_failed"
                    self._upload_error = str(err)
                else:
                    self._result = dict(coordinator.attributes)
                    return await self.async_step_done()

        schema = vol.Schema(
            {
                vol.Required(ATTR_FILE): FileSelector(
                    FileSelectorConfig(accept=".csv,text/csv")
                ),
            }
        )
        return self.async_show_form(
            step_id="upload",
            data_schema=schema,
            errors=errors,
            description_placeholders={"error": self._upload_error},
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        result = getattr(self, "_result", {}) or {}
        return self.async_show_form(
            step_id="done",
            data_schema=vol.Schema({}),
            description_placeholders={
                "source_file": str(result.get("source_file", "-")),
                "imported_hours": str(result.get("imported_hours", "-")),
                "imported_quarters": str(result.get("imported_quarters", "-")),
                "total_energy": str(result.get("total_energy", "-")),
                "last_quarter": str(result.get("last_import", "-")),
            },
        )
