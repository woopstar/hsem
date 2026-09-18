"""Track availability transitions for configured HSEM input entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import async_log


@dataclass(frozen=True, slots=True)
class EntityReference:
    """Identify a configured Home Assistant input entity."""

    config_key: str
    entity_id: str


class EntityAvailabilityTracker:
    """Log each configured input entity's unavailable and recovery transitions."""

    def __init__(self) -> None:
        """Initialise an empty per-entity availability history."""
        self._availability: dict[str, bool] = {}

    def track(self, hass: HomeAssistant, cfg: SensorConfig) -> None:
        """Observe configured entities and log availability state transitions."""
        references = configured_entity_references(cfg)
        configured_entity_ids = {reference.entity_id for reference in references}

        for reference in references:
            state = hass.states.get(reference.entity_id)
            available = state is not None and state.state not in {
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            }
            previous = self._availability.get(reference.entity_id)

            if previous is None:
                if not available:
                    self._log_unavailable(reference)
            elif previous and not available:
                self._log_unavailable(reference)
            elif not previous and available:
                async_log(
                    "info",
                    "Input entity %s (%s) is available again",
                    reference.entity_id,
                    reference.config_key,
                )

            self._availability[reference.entity_id] = available

        for entity_id in self._availability.keys() - configured_entity_ids:
            del self._availability[entity_id]

    @staticmethod
    def _log_unavailable(reference: EntityReference) -> None:
        """Log one unavailable transition for a configured entity."""
        async_log(
            "info",
            "Input entity %s (%s) became unavailable",
            reference.entity_id,
            reference.config_key,
        )


def configured_entity_references(cfg: SensorConfig) -> tuple[EntityReference, ...]:
    """Return active configured input entities with their config-entry keys."""
    references: dict[str, str] = {}

    def add(config_key: str, entity_id: str | None) -> None:
        if entity_id:
            references.setdefault(entity_id, config_key)

    direct_fields = (
        "huawei_solar_batteries_charging_cutoff_capacity",
        "huawei_solar_batteries_end_of_discharge_soc",
        "huawei_solar_batteries_excess_pv_energy_use_in_tou",
        "huawei_solar_batteries_forcible_charge",
        "huawei_solar_batteries_grid_charge_cutoff_soc",
        "huawei_solar_batteries_maximum_charging_power",
        "huawei_solar_batteries_maximum_discharging_power",
        "huawei_solar_batteries_rated_capacity",
        "huawei_solar_batteries_state_of_capacity",
        "huawei_solar_batteries_tou_charging_and_discharging_periods",
        "huawei_solar_batteries_working_mode",
        "huawei_solar_inverter_active_power_control",
        "house_consumption_power",
        "solar_production_power",
        "solcast_pv_forecast_forecast_today",
        "solcast_pv_forecast_forecast_tomorrow",
        "import_electricity_price_sensor",
        "export_electricity_price_sensor",
        "import_electricity_price_forecast_sensor",
        "export_electricity_price_forecast_sensor",
        "grid_import_energy_entity",
        "grid_export_energy_entity",
        "pv_energy_entity",
    )
    for field_name in direct_fields:
        add(f"hsem_{field_name}", getattr(cfg, field_name))

    if cfg.phase_aware_charging_enabled:
        for field_name in (
            "huawei_solar_batteries_charge_discharge_power",
            "huawei_solar_batteries_grid_charge_maximum_power",
            "huawei_solar_power_meter_phase_a_active_power",
            "huawei_solar_power_meter_phase_b_active_power",
            "huawei_solar_power_meter_phase_c_active_power",
        ):
            add(f"hsem_{field_name}", getattr(cfg, field_name))

    ev_fields = (
        ("hsem_ev_charger_status", cfg.ev.status_entity),
        ("hsem_ev_charger_power", cfg.ev.power_entity),
        ("hsem_ev_soc", cfg.ev.soc_entity),
        ("hsem_ev_connected", cfg.ev.connected_entity),
        ("hsem_ev_second_charger_status", cfg.ev_second.status_entity),
        ("hsem_ev_second_charger_power", cfg.ev_second.power_entity),
        ("hsem_ev_second_soc", cfg.ev_second.soc_entity),
        ("hsem_ev_second_connected", cfg.ev_second.connected_entity),
    )
    for config_key, entity_id in ev_fields:
        add(config_key, entity_id)

    if cfg.ml_consumption_enabled:
        add("hsem_ml_consumption_energy_entity", cfg.ml_consumption_energy_entity)
        add(
            "hsem_ml_consumption_temperature_entity",
            cfg.ml_consumption_temperature_entity,
        )
        add(
            "hsem_ml_consumption_weather_forecast_entity",
            cfg.ml_consumption_weather_forecast_entity,
        )

    return tuple(
        EntityReference(config_key=config_key, entity_id=entity_id)
        for entity_id, config_key in references.items()
    )
