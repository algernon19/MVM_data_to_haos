"""Config flow for the MVM Next Energy Import integration."""
from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import FileSelector, FileSelectorConfig

from .const import (
    ATTR_FILE,
    ATTR_FILENAME,
    CONF_IMPORT_DIR,
    DEFAULT_IMPORT_DIR,
    DOMAIN,
)


class MvmNextEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MVM Next Energy Import."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            import_dir = user_input[CONF_IMPORT_DIR].strip() or DEFAULT_IMPORT_DIR

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
                vol.Required(
                    CONF_IMPORT_DIR, default=DEFAULT_IMPORT_DIR
                ): str,
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
        return await self.async_step_upload(user_input)

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(
                self.config_entry.entry_id
            )
            if coordinator is None:
                errors["base"] = "not_loaded"
            else:
                filename = (user_input.get(ATTR_FILENAME) or "").strip() or None
                try:
                    await coordinator.async_upload(user_input[ATTR_FILE], filename)
                except HomeAssistantError as err:
                    errors["base"] = "upload_failed"
                    self._upload_error = str(err)
                except Exception:  # noqa: BLE001 - surface any parse/IO failure
                    errors["base"] = "upload_failed"
                else:
                    return self.async_create_entry(
                        title="", data=dict(self.config_entry.options)
                    )

        schema = vol.Schema(
            {
                vol.Required(ATTR_FILE): FileSelector(
                    FileSelectorConfig(accept=".csv,text/csv")
                ),
                vol.Optional(ATTR_FILENAME): str,
            }
        )
        return self.async_show_form(
            step_id="upload",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "error": getattr(self, "_upload_error", "") or ""
            },
        )
