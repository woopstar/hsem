"""Config flow steps that set HSEM up from a backup file (issue #1448).

When ``<config>/hsem_backups/`` holds a backup file, the
setup flow opens with a menu: set HSEM up from scratch, or restore.

- ``restore`` — pick a backup file. The file gets the checks
  ``hsem.import_data`` runs; a rejected file shows an error and nothing is
  written.
- ``restore_confirm`` — shows what the backup does not hold (redacted
  values) and which features stay off for that reason. Submitting writes the
  history files and creates the entry from the configuration in the backup.

All file work goes through :mod:`custom_components.hsem.services_backup`,
which runs it in the executor.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import selector

from custom_components.hsem.const import NAME
from custom_components.hsem.services_backup import (
    RestorePlan,
    async_find_backups,
    async_prepare_restore,
    async_write_restore,
)
from custom_components.hsem.utils.backup import BackupError
from custom_components.hsem.utils.schema import vol

_LOGGER = logging.getLogger(__name__)

#: Form field of the ``restore`` step.
CONF_BACKUP_FILE = "backup_file"

#: Shown in the confirm step for an empty list.
_NONE = "-"


def _listed(values: list[str]) -> str:
    """Return *values* as one line for a step description."""
    return ", ".join(values) or _NONE


class HSEMRestoreFlow(config_entries.ConfigFlow):
    """The restore steps of the HSEM setup flow."""

    #: Set once the menu was shown, so choosing "set up" shows the first form.
    _restore_offered: bool = False

    #: The validated backup the confirm step creates the entry from.
    _restore_plan: RestorePlan | None = None

    async def _async_offer_restore(self) -> ConfigFlowResult | None:
        """Return the setup-or-restore menu when a backup file exists.

        A file that cannot be restored counts too: the restore step shows why
        it is rejected.

        Returns:
            The menu, or ``None`` when the normal setup form is to be shown:
            there is no backup file, or the menu was shown already.
        """
        if self._restore_offered:
            return None
        self._restore_offered = True
        backups = await async_find_backups(self.hass, self.VERSION, self.MINOR_VERSION)
        if not backups:
            return None
        return await self.async_step_setup_method()

    async def async_step_setup_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu: set HSEM up from scratch, or restore a backup."""
        return self.async_show_menu(
            step_id="setup_method", menu_options=["user", "restore"]
        )

    async def async_step_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a backup file, and validate it.

        Nothing is written in this step.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            filename = str(user_input.get(CONF_BACKUP_FILE, ""))
            try:
                self._restore_plan = await async_prepare_restore(
                    self.hass, filename, self.VERSION, self.MINOR_VERSION
                )
            except BackupError as err:
                _LOGGER.warning("Backup %s cannot be restored: %s", filename, err)
                errors["base"] = err.code
            else:
                return await self.async_step_restore_confirm()

        backups = await async_find_backups(self.hass, self.VERSION, self.MINOR_VERSION)
        if not backups:
            return self.async_abort(reason="no_backups")
        default = next((name for name, code in backups.items() if code is None), None)
        field = (
            vol.Required(CONF_BACKUP_FILE)
            if default is None
            else vol.Required(CONF_BACKUP_FILE, default=default)
        )
        return self.async_show_form(
            step_id="restore",
            data_schema=vol.Schema(
                {
                    field: selector(
                        {"select": {"options": list(backups), "mode": "dropdown"}}
                    )
                }
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_restore_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what the backup restores, then write it and create the entry."""
        plan = self._restore_plan
        if plan is None:
            return await self.async_step_restore()
        errors: dict[str, str] = {}
        configuration = plan.configuration

        if user_input is not None:
            try:
                before_path = await async_write_restore(self.hass, plan)
            except BackupError as err:
                _LOGGER.warning("Backup %s was not restored: %s", plan.filename, err)
                errors["base"] = err.code
            else:
                return self.async_create_entry(
                    title=str(configuration.data.get("device_name") or NAME),
                    data=configuration.data,
                    options=configuration.options,
                    description="history_saved" if before_path else None,
                    description_placeholders={"before_path": str(before_path)},
                )

        return self.async_show_form(
            step_id="restore_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "backup_file": plan.filename,
                "created": str(plan.payload.get("created") or _NONE),
                "hsem_version": str(plan.payload.get("hsem_version") or _NONE),
                "skipped_keys": _listed(configuration.skipped_keys),
                "disabled_features": _listed(configuration.disabled_features),
                "missing_devices": _listed(plan.missing_devices),
                "history_files_skipped": _listed(sorted(plan.files_skipped)),
            },
            last_step=True,
        )
