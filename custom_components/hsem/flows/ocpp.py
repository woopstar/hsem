"""Config flow step for the embedded OCPP 1.6 server.

This step is only shown when the user has EV planned load enabled and
wants to use OCPP-based charger control instead of (or in addition to)
entity-based control via Home Assistant entities.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_ocpp_step_schema(
    config_entry: ConfigEntry | None,
    *,
    second_ev_enabled: bool = False,
) -> vol.Schema:
    """Return the data schema for the OCPP server config flow step.

    Args:
        config_entry: Active config entry; ``None`` for the initial config
            flow.
        second_ev_enabled: When ``True``, include the second OCPP server
            fields (for the optional second EV).  The second server can only
            be configured when the second EV is enabled.

    Returns:
        A ``vol.Schema`` for the OCPP step.
    """
    fields: dict = {
        vol.Required(
            "hsem_ocpp_enabled",
            default=bool(get_config_value(config_entry, "hsem_ocpp_enabled")),
        ): selector({"boolean": {}}),
        vol.Required(
            "hsem_ocpp_port",
            default=get_config_value(config_entry, "hsem_ocpp_port"),
        ): selector(
            {
                "number": {
                    "min": 1024,
                    "max": 65535,
                    "step": 1,
                    "mode": "box",
                }
            }
        ),
        vol.Optional(
            "hsem_ocpp_cpid",
            default=get_config_value(config_entry, "hsem_ocpp_cpid") or "",
        ): str,
        vol.Required(
            "hsem_ocpp_start_window_s",
            default=get_config_value(config_entry, "hsem_ocpp_start_window_s"),
        ): selector(
            {
                "number": {
                    "min": 0,
                    "max": 600,
                    "step": 10,
                    "unit_of_measurement": "s",
                    "mode": "slider",
                }
            }
        ),
        vol.Required(
            "hsem_ocpp_stop_window_s",
            default=get_config_value(config_entry, "hsem_ocpp_stop_window_s"),
        ): selector(
            {
                "number": {
                    "min": 0,
                    "max": 1800,
                    "step": 30,
                    "unit_of_measurement": "s",
                    "mode": "slider",
                }
            }
        ),
    }

    if second_ev_enabled:
        # Second OCPP server — only configurable when the second EV is
        # enabled. Runs on its own port so each EV gets a dedicated server.
        fields[
            vol.Required(
                "hsem_ocpp_second_enabled",
                default=bool(
                    get_config_value(config_entry, "hsem_ocpp_second_enabled")
                ),
            )
        ] = selector({"boolean": {}})
        fields[
            vol.Required(
                "hsem_ocpp_second_port",
                default=get_config_value(config_entry, "hsem_ocpp_second_port"),
            )
        ] = selector(
            {
                "number": {
                    "min": 1024,
                    "max": 65535,
                    "step": 1,
                    "mode": "box",
                }
            }
        )
        fields[
            vol.Optional(
                "hsem_ocpp_second_cpid",
                default=get_config_value(config_entry, "hsem_ocpp_second_cpid") or "",
            )
        ] = str

    return vol.Schema(fields)


async def validate_ocpp_step_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the OCPP server flow step.

    Args:
        hass: Home Assistant instance.
        user_input: Dict of field name → value submitted by the user.

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    errors: dict[str, str] = {}

    required_fields = [
        "hsem_ocpp_enabled",
        "hsem_ocpp_port",
        "hsem_ocpp_start_window_s",
        "hsem_ocpp_stop_window_s",
    ]

    # Second-server fields are only required when submitted; they are only
    # presented when the second EV is enabled.
    if "hsem_ocpp_second_enabled" in user_input:
        required_fields.extend(["hsem_ocpp_second_port"])

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    # Validate port range
    port = user_input.get("hsem_ocpp_port")
    if port is not None:
        try:
            port_int = int(port)
            if port_int < 1024 or port_int > 65535:
                errors["hsem_ocpp_port"] = "power_out_of_range"
        except ValueError, TypeError:
            errors["hsem_ocpp_port"] = "invalid_power_value"

    second_port = user_input.get("hsem_ocpp_second_port")
    if second_port is not None:
        try:
            second_port_int = int(second_port)
            if second_port_int < 1024 or second_port_int > 65535:
                errors["hsem_ocpp_second_port"] = "power_out_of_range"
            elif (
                port is not None
                and "hsem_ocpp_port" not in errors
                and second_port_int == int(port)
            ):
                errors["hsem_ocpp_second_port"] = "port_conflict"
        except ValueError, TypeError:
            errors["hsem_ocpp_second_port"] = "invalid_power_value"

    return errors
