"""Config flow for the MVM Next Energy Import integration."""
from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_IMPORT_DIR, DEFAULT_IMPORT_DIR, DOMAIN


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
    def _ensure_dir(path: str) -> bool:
        """Create the import directory if needed. Returns False on failure."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return False
        return os.path.isdir(path)
