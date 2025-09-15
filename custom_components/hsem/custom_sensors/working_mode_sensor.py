"""
This module defines the HSEMWorkingModeSensorNew class, which is a custom sensor entity for Home Assistant.
The sensor monitors various attributes related to solar energy production, battery status, and energy consumption,
and calculates the optimal working mode for the system.

Classes:
    HSEMWorkingModeSensorNew(SensorEntity, HSEMEntity): Represents a custom sensor entity for monitoring and optimizing
    solar energy production and consumption.
"""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import State
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

from custom_components.hsem.const import (
    BASELINE_7D_SHARE,
    BASELINE_14D_SHARE,
    CAP7_DOWN,
    CAP7_UP,
    CAP14_DOWN,
    CAP14_UP,
    CHANGE3_LIMIT_DOWN_FACTOR,
    CHANGE3_LIMIT_UP_FACTOR,
    CHANGE_LIMIT_DOWN_FACTOR,
    CHANGE_LIMIT_UP_FACTOR,
    DEFAULT_CONFIG_VALUES,
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
    DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE,
    RELIABILITY_EPS,
    RELIABILITY_SCALE_STRENGTH,
    SPIKE1_RATIO_MAX,
    SPIKE1_RATIO_MIN,
    SPIKE1_REDIST_TO_3D,
    SPIKE1_REDIST_TO_7D,
    SPIKE1_REDIST_TO_14D,
    SPIKE1_REDUCE_FRACTION_MAX,
    SPIKE3_RATIO_MAX,
    SPIKE3_RATIO_MIN,
    SPIKE3_REDIST_TO_7D,
    SPIKE3_REDIST_TO_14D,
    SPIKE3_REDUCE_FRACTION_MAX,
    SPIKE7_RATIO_MAX,
    SPIKE7_RATIO_MIN,
    SPIKE7_REDIST_TO_14D,
    SPIKE7_REDUCE_FRACTION_MAX,
    SPIKE14_RATIO_MAX,
    SPIKE14_RATIO_MIN,
    SPIKE14_REDIST_TO_7D,
    SPIKE14_REDUCE_FRACTION_MAX,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.models.battery_schedule import BatterySchedule
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.utils.huawei import (
    async_set_grid_export_power_pct,
    async_set_tou_periods,
)
from custom_components.hsem.utils.misc import (
    async_logger,
    async_resolve_entity_id_from_unique_id,
    async_set_number_value,
    async_set_select_option,
    convert_to_boolean,
    convert_to_float,
    convert_to_int,
    convert_to_time,
    generate_hash,
    get_config_value,
    get_max_discharge_power,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_unique_id,
    get_working_mode_sensor_entity_id,
    get_working_mode_sensor_name,
    get_working_mode_sensor_unique_id,
)
from custom_components.hsem.utils.workingmodes import WorkingModes


class HSEMWorkingModeSensor(SensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True

    # Exclude all attributes from recording except standard ones
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, config_entry) -> None:
        super().__init__(config_entry)

        # set config entry and state
        self._config_entry = config_entry
        self._state = None
        self._hsem_verbose_logging = True

        # Initialize all attributes to None or some default value
        self._read_only = False
        self._available = False
        self._update_interval = 1
        self._timer = None
        self._timer_interval = None

        self._hourly_recommendations = []
        self._batteries_schedules = []

        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._name = get_working_mode_sensor_name()
        self._tz = None
        self._last_updated = None
        self._next_update = None
        self._hsem_avg_house_consumption_entity_id_cache = {}
        # Number of minutes per recommendation interval (e.g., 60 means hourly intervals)
        self._recommendation_interval_minutes = 15
        # Total number of intervals to generate recommendations for (e.g., 48 means 2 days of hourly intervals)
        self._recommendation_interval_length = 48

        self._tracked_entities = set()
        self._update_settings()

        # Initialize all attributes to None or some default value
        self._batteries_schedules_remaining_capacity_needed = 0.0
        self._hsem_extended_attributes = False
        self._hsem_huawei_solar_device_id_inverter_1 = None
        self._hsem_huawei_solar_device_id_inverter_2 = None
        self._hsem_huawei_solar_device_id_batteries = None
        self._hsem_huawei_solar_batteries_working_mode = None
        self._hsem_huawei_solar_batteries_state_of_capacity = None
        self._hsem_house_consumption_power = None
        self._hsem_solar_production_power = None
        self._hsem_ev_charger_status = None
        self._hsem_ev_charger_power = None
        self._hsem_solcast_pv_forecast_forecast_today = None
        self._hsem_energi_data_service_import = None
        self._hsem_energi_data_service_export = None
        self._hsem_energi_data_service_export_min_price = None
        self._hsem_huawei_solar_inverter_active_power_control = None
        self._hsem_huawei_solar_batteries_working_mode_state = None
        self._hsem_huawei_solar_batteries_state_of_capacity_state = None
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc = None
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state = None
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods = None
        self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou_state = None
        self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou = None
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
            None
        )
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = (
            None
        )
        self._hsem_house_power_includes_ev_charger_power = None
        self._hsem_ev_charger_status_state = False
        self._hsem_ev_charger_power_state = False
        self._hsem_ev_charger_force_max_discharge_power = None
        self._hsem_ev_charger_max_discharge_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_solar_production_power_state = 0.0
        self._hsem_huawei_solar_inverter_active_power_control_state = None
        self._hsem_net_consumption = 0.0
        self._hsem_net_consumption_with_ev = 0.0
        self._hsem_huawei_solar_batteries_maximum_charging_power = None
        self._hsem_huawei_solar_batteries_maximum_charging_power_state = None
        self._hsem_huawei_solar_batteries_maximum_discharging_power = None
        self._hsem_huawei_solar_batteries_maximum_discharging_power_state = None
        self._hsem_batteries_conversion_loss = 0.0
        self._hsem_batteries_usable_capacity = 0.0
        self._hsem_batteries_current_capacity = 0.0
        self._hsem_energi_data_service_import_state = 0.0
        self._hsem_energi_data_service_export_state = 0.0
        self._hsem_house_consumption_energy_weight_1d = 50
        self._hsem_house_consumption_energy_weight_3d = 20
        self._hsem_house_consumption_energy_weight_7d = 15
        self._hsem_house_consumption_energy_weight_14d = 10
        self._hsem_batteries_rated_capacity_min_state = None
        self._hsem_batteries_rated_capacity_max = None
        self._hsem_batteries_rated_capacity_max_state = 0.0
        self._energy_needs = {
            "0am_6am": 0.0,
            "6am_10am": 0.0,
            "10am_5pm": 0.0,
            "5pm_9pm": 0.0,
            "9pm_midnight": 0.0,
        }

        self._missing_input_entities = True
        self._missing_input_entities_list = []
        self._hsem_batteries_enable_batteries_schedule_1 = False
        self._hsem_batteries_enable_batteries_schedule_1_start = None
        self._hsem_batteries_enable_batteries_schedule_1_end = None
        self._hsem_batteries_enable_batteries_schedule_1_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_1_min_price_difference = 0.0
        self._hsem_batteries_enable_batteries_schedule_2 = False
        self._hsem_batteries_enable_batteries_schedule_2_start = None
        self._hsem_batteries_enable_batteries_schedule_2_end = None
        self._hsem_batteries_enable_batteries_schedule_2_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_2_min_price_difference = 0.0
        self._hsem_batteries_enable_batteries_schedule_3 = False
        self._hsem_batteries_enable_batteries_schedule_3_start = None
        self._hsem_batteries_enable_batteries_schedule_3_end = None
        self._hsem_batteries_enable_batteries_schedule_3_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_3_min_price_difference = 0.0
        self._hsem_force_working_mode = None
        self._hsem_force_working_mode_state = "auto"
        self._hsem_solcast_pv_forecast_forecast_likelihood = None
        self._hsem_months_winter = []
        self._hsem_months_summer = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""

        if self._missing_input_entities:
            return {
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state yet. Check your configuration and make sure input sensors are configured correctly.",
                "missing_input_entities_list": self._missing_input_entities_list,
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        if not self._available:
            return {
                "status": "wait",
                "description": "Waiting for sensor to be available.",
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        extended_attributes = {}
        if self._hsem_extended_attributes:
            extended_attributes = {
                "energi_data_service_export_entity": self._hsem_energi_data_service_export,
                "energi_data_service_import_entity": self._hsem_energi_data_service_import,
                "ev_charger_power_entity": self._hsem_ev_charger_power,
                "ev_charger_status_entity": self._hsem_ev_charger_status,
                "force_working_mode_entity": self._hsem_force_working_mode,
                "house_consumption_power_entity": self._hsem_house_consumption_power,
                "huawei_solar_batteries_grid_charge_cutoff_soc_entity": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
                "huawei_solar_batteries_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
                "huawei_solar_batteries_maximum_discharging_power_entity": self._hsem_huawei_solar_batteries_maximum_discharging_power,
                "huawei_solar_batteries_rated_capacity_max_entity": self._hsem_batteries_rated_capacity_max,
                "huawei_solar_batteries_state_of_capacity_entity": self._hsem_huawei_solar_batteries_state_of_capacity,
                "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
                "huawei_solar_batteries_working_mode_entity": self._hsem_huawei_solar_batteries_working_mode,
                "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
                "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
                "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
                "huawei_solar_inverter_active_power_control_state_entity": self._hsem_huawei_solar_inverter_active_power_control,
                "next_update": self._next_update,
                "read_only": self._read_only,
                "solar_production_power_entity": self._hsem_solar_production_power,
                "solcast_pv_forecast_forecast_today_entity": self._hsem_solcast_pv_forecast_forecast_today,
                "unique_id": self._attr_unique_id,
                "update_interval": self._update_interval,
            }

        attributes = {
            "batteries_conversion_loss": self._hsem_batteries_conversion_loss,
            "batteries_current_capacity": self._hsem_batteries_current_capacity,
            "batteries_usable_capacity": self._hsem_batteries_usable_capacity,
            "energi_data_service_export_state": self._hsem_energi_data_service_export_state,
            "energi_data_service_import_state": self._hsem_energi_data_service_import_state,
            "energi_data_service_export_min_price": self._hsem_energi_data_service_export_min_price,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "ev_charger_max_discharge_power_state": self._hsem_ev_charger_max_discharge_power,
            "ev_charger_force_max_discharge_power": self._hsem_ev_charger_force_max_discharge_power,
            "force_working_mode_state": self._hsem_force_working_mode_state,
            "hourly_recommendations": self._hourly_recommendations,
            "house_consumption_energy_weight_14d": self._hsem_house_consumption_energy_weight_14d,
            "house_consumption_energy_weight_1d": self._hsem_house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": self._hsem_house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": self._hsem_house_consumption_energy_weight_7d,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "batteries_schedules_remaining_capacity_needed": self._batteries_schedules_remaining_capacity_needed,
            "batteries_schedules": self._batteries_schedules,
            "huawei_solar_batteries_grid_charge_cutoff_soc_state": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
            "huawei_solar_batteries_maximum_charging_power_state": self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            "huawei_solar_batteries_maximum_discharging_power_state": self._hsem_huawei_solar_batteries_maximum_discharging_power_state,
            "huawei_solar_batteries_rated_capacity_max_state": self._hsem_batteries_rated_capacity_max_state,
            "huawei_solar_batteries_rated_capacity_min_state": self._hsem_batteries_rated_capacity_min_state,
            "huawei_solar_batteries_state_of_capacity_state": self._hsem_huawei_solar_batteries_state_of_capacity_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state,
            "huawei_solar_batteries_working_mode_state": self._hsem_huawei_solar_batteries_working_mode_state,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "huawei_solar_batteries_excess_pv_energy_use_in_tou_state": self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou_state,
            "solcast_pv_forecast_forecast_likelihood": self._hsem_solcast_pv_forecast_forecast_likelihood,
            "last_updated": self._last_updated,
            "net_consumption_with_ev": self._hsem_net_consumption_with_ev,
            "net_consumption": self._hsem_net_consumption,
            "solar_production_power_state": self._hsem_solar_production_power_state,
            "months_winter": self._hsem_months_winter,
            "months_summer": self._hsem_months_summer,
        }

        status = {
            "status": "ok",
        }
        if self._read_only:
            status = {
                "status": "read_only",
            }

        # Return sorted attributes
        return {
            key: value
            for key, value in sorted(
                {**attributes, **extended_attributes, **status}.items()
            )
        }

    def _generate_recommendation_intervals(
        self, interval_minutes: int, total_hours: int
    ) -> list[HourlyRecommendation]:
        now = datetime.now().astimezone(self._tz)

        # Rund ned til nærmeste hele interval
        # minutes_since_hour = floor(now.minute / interval_minutes) * interval_minutes
        # start_time = now.replace(minute=minutes_since_hour, second=0, microsecond=0)
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)

        intervals = []
        steps = int((total_hours * 60) / interval_minutes)

        for i in range(steps):
            interval_start = start_time + timedelta(minutes=i * interval_minutes)
            interval_end = interval_start + timedelta(minutes=interval_minutes)

            recommendation = HourlyRecommendation(
                avg_house_consumption=0.0,
                batteries_charged=0.0,
                end=interval_end,
                estimated_battery_capacity=0.0,
                estimated_battery_soc=0,
                estimated_cost=0.0,
                estimated_net_consumption=0.0,
                export_price=0.0,
                import_price=0.0,
                recommendation=None,
                solcast_pv_estimate=0.0,
                start=interval_start,
            )

            intervals.append(recommendation)

        return intervals

    async def _set_update_interval(self, override_interval=None) -> None:
        if override_interval:
            interval = timedelta(minutes=override_interval)
        else:
            interval = timedelta(minutes=self._update_interval)

        # only re-register if changed
        if self._timer_interval != interval:
            self._timer_interval = interval
            await self._async_register_timer(interval)

        self._next_update = (datetime.now().astimezone(self._tz) + interval).isoformat()

    async def _async_handle_update(self, event=None) -> None:
        """Handle the sensor state update (for both manual and state change)."""

        await async_logger(self, f"------ Updating {self._name} state...")

        now = datetime.now().astimezone(self._tz)

        self._update_settings()

        # Reset recommendations
        self._hourly_recommendations = self._generate_recommendation_intervals(
            self._recommendation_interval_minutes, self._recommendation_interval_length
        )

        await self._async_fetch_entity_states()

        await self._async_setup_batteries_schedules()

        # self._batteries_schedules = self._generate_batteries_schedules()
        self._batteries_schedules.sort(key=lambda x: x.start)

        if not await self._async_calculate_avg_house_consumption():
            self._missing_input_entities = True

        if self._missing_input_entities:
            await self._set_update_interval(1)
        else:
            await self._set_update_interval()

        if (
            self._missing_input_entities
            and self._hsem_force_working_mode_state == "auto"
        ):
            self._state = Recommendations.MissingInputEntities.value

            await async_logger(self, "Missing input entities, skipping calculations.")
        elif self._hsem_force_working_mode_state != "auto":
            self._state = str(self._hsem_force_working_mode_state)

            await async_logger(
                self,
                f"Force working mode is activated. Setting working mode to {str(self._hsem_force_working_mode_state)}",
            )
        else:

            await self._async_calculate_net_consumption()

            await self._async_calculate_remaining_battery_capacity()

            # Manipulate all the recommendations.
            share = 1

            for attr in ["forecast", "raw_tomorrow", "raw_today"]:
                await self._async_update_hourly_data(
                    "sensor.energi_data_service",
                    "import_price",
                    "price",
                    attr,
                    "hour",
                    share,
                )

                await self._async_update_hourly_data(
                    "sensor.energi_data_service_produktion",
                    "export_price",
                    "price",
                    attr,
                    "hour",
                    share,
                )

            if self._hsem_solcast_pv_forecast_forecast_likelihood is None:
                self._hsem_solcast_pv_forecast_forecast_likelihood = (
                    DEFAULT_CONFIG_VALUES[
                        "hsem_solcast_pv_forecast_forecast_likelihood"
                    ]
                )

            share = 60 / self._recommendation_interval_minutes

            await self._async_update_hourly_data(
                "sensor.solcast_pv_forecast_forecast_today",
                "solcast_pv_estimate",
                self._hsem_solcast_pv_forecast_forecast_likelihood,
                "detailedForecast",
                "period_start",
                share,
            )

            await self._async_update_hourly_data(
                "sensor.solcast_pv_forecast_forecast_tomorrow",
                "solcast_pv_estimate",
                self._hsem_solcast_pv_forecast_forecast_likelihood,
                "detailedForecast",
                "period_start",
                share,
            )

            await self._async_calculate_hourly_net_consumption()

            await self._async_calculate_hourly_estimated_cost()

            await self._async_calculate_estimated_batteries_capacity()

            midnights = [
                r
                for r in self._hourly_recommendations
                if r.start.time() == time(0, 0, 0)
            ]

            for r in midnights:
                if r.start < datetime.now().astimezone(self._tz):
                    await self._async_calculate_batteries_schedules()
                    await self._async_calculate_batteries_schedules_best_charge_time()
                else:
                    await self._async_calculate_batteries_schedules(
                        r.start.astimezone(self._tz)
                    )
                    await self._async_calculate_batteries_schedules_best_charge_time(
                        r.start.astimezone(self._tz)
                    )

            await self._async_set_time_passed()

            await self._async_optimization_strategy()

            # Get the current recommendation we need to work from by sorting on time.
            self._hourly_recommendations.sort(key=lambda x: x.start)
            hourly_recommendation = next(
                (
                    rec
                    for rec in self._hourly_recommendations
                    if rec.start.astimezone(self._tz)
                    <= now
                    < rec.end.astimezone(self._tz)
                ),
                None,
            )

            await self._async_update_current_hourly_recommendation(
                hourly_recommendation
            )

            await async_logger(
                self, f"Current hourly recommendation: {hourly_recommendation}"
            )

            if not self._read_only:
                await self._async_set_inverter_power_control()
                await self._async_set_inverter_and_batteries_settings(
                    hourly_recommendation
                )

            if hourly_recommendation:
                self._state = hourly_recommendation.recommendation
            else:
                self._state = None

        # Final sorting
        self._hourly_recommendations.sort(key=lambda x: x.start)

        # Update last update time
        self._last_updated = now.isoformat()
        self._available = True

        await async_logger(self, f"------ Completed updating {self._name} state...")

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        await self._async_handle_update(event)

    async def async_options_updated(self, config_entry) -> None:
        """Handle options update from configuration change."""
        await self._async_handle_update(None)

    async def _async_register_timer(self, interval: timedelta):
        # cancel old timer if any
        if self._timer:
            self._timer()
            self._timer = None

        # register new one
        self._timer = async_track_time_interval(
            self.hass, self._async_handle_update, interval
        )

        await async_logger(self, f"Updating HSEM with interval: {interval}.")

    async def async_added_to_hass(self) -> None:
        """Handle the sensor being added to Home Assistant."""

        # Initialize timezone now that hass is available
        self._tz = ZoneInfo(str(self.hass.config.time_zone))

        # Initial update
        await self._async_handle_update(None)

        # Schedule updates at the start of every hour
        async_track_time_change(
            self.hass,
            self._async_handle_update,
            hour="*",
            minute=0,
            second=10,
        )

        await super().async_added_to_hass()

    async def _async_update_current_hourly_recommendation(
        self, hourly_recommendation
    ) -> None:

        # Negative import price. Force export everyting to earn money.
        if convert_to_float(self._hsem_energi_data_service_import_state) < 0:
            hourly_recommendation.recommendation = Recommendations.ForceExport.value

        # EV is charging
        elif self._hsem_ev_charger_status_state:
            hourly_recommendation.recommendation = Recommendations.EVSmartCharging.value

        # If we have more capacity on our battery to cover the remaining schedules, lets change to discharge mode.
        elif (
            self._batteries_schedules_remaining_capacity_needed > 0
            and self._hsem_batteries_current_capacity
            > self._batteries_schedules_remaining_capacity_needed
        ):
            hourly_recommendation.recommendation = (
                Recommendations.BatteriesDischargeMode.value
            )

    async def _async_set_inverter_and_batteries_settings(
        self, hourly_recommendation
    ) -> None:
        tou_modes = None
        working_mode = None

        # Set maximum discharging power for batteries if EV charger is not active
        max_discharge_power = get_max_discharge_power(
            convert_to_int(self._hsem_batteries_rated_capacity_max_state)
        )

        if not self._hsem_ev_charger_status_state:
            if (
                self._hsem_huawei_solar_batteries_maximum_discharging_power_state
                != max_discharge_power
            ):
                await async_set_number_value(
                    self,
                    self._hsem_huawei_solar_batteries_maximum_discharging_power,
                    max_discharge_power,
                )

        # Set mode based upon recommendation
        match hourly_recommendation.recommendation:
            case Recommendations.ForceExport.value:
                working_mode = WorkingModes.FullyFedToGrid.value
            case Recommendations.BatteriesChargeGrid.value:
                tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
                working_mode = WorkingModes.TimeOfUse.value
            case Recommendations.EVSmartCharging.value:
                if self._hsem_ev_charger_force_max_discharge_power:
                    working_mode = WorkingModes.MaximizeSelfConsumption.value
                else:
                    tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
                    working_mode = WorkingModes.TimeOfUse.value
            case Recommendations.BatteriesDischargeMode.value:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
            case Recommendations.BatteriesChargeSolar.value:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
            case Recommendations.ForceBatteriesDischarge.value:
                tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE
                working_mode = WorkingModes.TimeOfUse.value
            case Recommendations.BatteriesWaitMode.value:
                tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
                working_mode = WorkingModes.TimeOfUse.value
            case _:
                return

        if (
            hourly_recommendation.recommendation
            == Recommendations.EVSmartCharging.value
            and self._hsem_ev_charger_force_max_discharge_power
        ):

            # Set maximum discharging power for batteries
            if (
                self._hsem_huawei_solar_batteries_maximum_discharging_power_state
                != self._hsem_ev_charger_max_discharge_power
            ):
                await async_set_number_value(
                    self,
                    self._hsem_huawei_solar_batteries_maximum_discharging_power,
                    self._hsem_ev_charger_max_discharge_power,
                )

        # Set pv excess in tou
        if (
            hourly_recommendation.recommendation
            == Recommendations.BatteriesWaitMode.value
            or hourly_recommendation.recommendation == WorkingModes.FullyFedToGrid.value
        ):
            if (
                self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou_state
                != "fed_to_grid"
            ):
                await async_set_select_option(
                    self,
                    self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou,
                    "fed_to_grid",
                )
        else:
            if (
                self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou_state
                != "charge"
            ):
                await async_set_select_option(
                    self,
                    self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou,
                    "charge",
                )

        # Apply TOU periods if working mode is TOU
        if working_mode == WorkingModes.TimeOfUse.value and tou_modes:
            if generate_hash(str(tou_modes)) != generate_hash(
                str(
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods
                )
            ):
                await async_set_tou_periods(
                    self, self._hsem_huawei_solar_device_id_batteries, tou_modes
                )

        # Only apply working mode if it has changed
        if (
            working_mode
            and self._hsem_huawei_solar_batteries_working_mode_state != working_mode
        ):
            await async_set_select_option(
                self, self._hsem_huawei_solar_batteries_working_mode, working_mode
            )

    async def _async_calculate_estimated_batteries_capacity(self) -> None:
        """Calculate the estimated battery capacity for each hour based on net consumption and charging."""

        self._batteries_schedules.sort(key=lambda x: x.start)

        previous_capacity = 0.0
        for obj in self._hourly_recommendations:
            if (
                obj.start.astimezone(self._tz)
                <= datetime.now().astimezone(self._tz)
                < obj.end.astimezone(self._tz)
            ):
                obj.estimated_battery_capacity = max(
                    self._hsem_batteries_current_capacity
                    - obj.estimated_net_consumption
                    + obj.batteries_charged,
                    0.0,
                )

                if (
                    obj.estimated_battery_capacity
                    > self._hsem_batteries_usable_capacity
                ):
                    obj.estimated_battery_capacity = (
                        self._hsem_batteries_usable_capacity
                    )

                obj.estimated_battery_capacity = round(
                    obj.estimated_battery_capacity, 3
                )

                previous_capacity = obj.estimated_battery_capacity

            elif obj.start.astimezone(self._tz) >= datetime.now().astimezone(self._tz):
                obj.estimated_battery_capacity = max(
                    previous_capacity
                    - obj.estimated_net_consumption
                    + obj.batteries_charged,
                    0.0,
                )

                if (
                    obj.estimated_battery_capacity
                    > self._hsem_batteries_usable_capacity
                ):
                    obj.estimated_battery_capacity = (
                        self._hsem_batteries_usable_capacity
                    )

                obj.estimated_battery_capacity = round(
                    obj.estimated_battery_capacity, 3
                )

                previous_capacity = obj.estimated_battery_capacity

        # Calculate estimated battery state of charge (SoC) as a percentage
        for obj in self._hourly_recommendations:
            if obj.estimated_battery_capacity > 0:
                obj.estimated_battery_soc = round(
                    obj.estimated_battery_capacity
                    / self._hsem_batteries_usable_capacity
                    * 100,
                    2,
                )

    async def _async_set_time_passed(self) -> None:
        """
        Mark hourly recommendation intervals that have already passed as 'TimePassed'.
        This indicates that for these intervals, the battery or system cannot take further action,
        and recommendations for those times are no longer relevant.
        """

        await async_logger(
            self, "Starting marking recommendations already passed as Time Passed."
        )

        for obj in self._hourly_recommendations:
            # Mark recommendations as "TimePassed" for intervals before the current time,
            # indicating these time slots are no longer actionable.
            if obj.end.astimezone(self._tz) < datetime.now().astimezone(self._tz):
                obj.recommendation = Recommendations.TimePassed.value

    async def _async_calculate_hourly_net_consumption(self) -> None:
        """Calculate the estimated net consumption for each hour of the day."""

        for obj in self._hourly_recommendations:
            if obj.avg_house_consumption is None:
                continue

            if obj.solcast_pv_estimate:
                obj.estimated_net_consumption = round(
                    obj.avg_house_consumption - obj.solcast_pv_estimate, 3
                )
            else:
                obj.estimated_net_consumption = round(obj.avg_house_consumption, 3)

        await async_logger(
            self, "Updated hourly calculations with Estimated Net Consumption"
        )

    async def _async_calculate_hourly_estimated_cost(self) -> None:
        """Calculate the estimated cost for each hour of the day based on net consumption and import/export prices."""

        for obj in self._hourly_recommendations:
            if obj.estimated_net_consumption is None:
                continue

            if obj.import_price is None or obj.export_price is None:
                continue

            if obj.estimated_net_consumption > 0:
                obj.estimated_cost = round(
                    obj.estimated_net_consumption * obj.import_price, 3
                )
            else:
                obj.estimated_cost = round(
                    obj.estimated_net_consumption * obj.export_price, 3
                )

        await async_logger(self, "Updated hourly calculations with Estimated Cost")

    async def _async_update_hourly_data(
        self,
        sensor_id: str,
        object_attr: str,
        sensor_field: str,
        sensor_list_attr: str,
        key_field: str,
        share: float,
    ) -> None:

        sensor_state = self.hass.states.get(sensor_id)

        if not sensor_state:
            await async_logger(
                self, f"Input sensor {sensor_id} was not found for data."
            )
            return

        sensor_data = sensor_state.attributes.get(sensor_list_attr) or []
        if not sensor_data:
            return

        await async_logger(self, f"Updating data for {object_attr}...")

        for data in sensor_data:
            v = data.get(key_field)
            if not v:
                continue

            if isinstance(v, datetime):
                dt_key = v
            else:
                dt_key = datetime.fromisoformat(str(v))

            try:
                dt_key = dt_key.replace(minute=0, second=0, microsecond=0).astimezone(
                    self._tz
                )
            except Exception as e:
                continue

            value = convert_to_float(data.get(sensor_field))
            if value is None:
                continue

            # Adjust value based on share (e.g., if data is in smaller intervals)
            value = value / share

            for obj in self._hourly_recommendations:
                obj_hour = obj.start.replace(
                    minute=0, second=0, microsecond=0
                ).astimezone(self._tz)

                if obj.start.date() == dt_key.date() and obj_hour == dt_key:
                    setattr(obj, object_attr, round(value, 3))

    async def _async_calculate_avg_house_consumption(self) -> bool:
        """Calculate the weighted hourly data for the sensor using 1/3/7/14-day HouseConsumptionEnergyAverageSensors,
        with spike-aware dynamic reweighting, capping of 1d/3d/7d/14d vs baseline, and reliability-based weight scaling.
        """

        if self._hsem_house_consumption_energy_weight_1d is None:
            self._missing_input_entities_list.append(
                str(self._hsem_house_consumption_energy_weight_1d)
            )
            await async_logger(
                self, "Weight for 1d is None. Skipping this calculation."
            )
            return False

        if self._hsem_house_consumption_energy_weight_3d is None:
            self._missing_input_entities_list.append(
                str(self._hsem_house_consumption_energy_weight_3d)
            )
            await async_logger(
                self, "Weight for 3d is None. Skipping this calculation."
            )
            return False

        if self._hsem_house_consumption_energy_weight_7d is None:
            self._missing_input_entities_list.append(
                str(self._hsem_house_consumption_energy_weight_7d)
            )
            await async_logger(
                self, "Weight for 7d is None. Skipping this calculation."
            )
            return False

        if self._hsem_house_consumption_energy_weight_14d is None:
            self._missing_input_entities_list.append(
                str(self._hsem_house_consumption_energy_weight_14d)
            )
            await async_logger(
                self, "Weight for 14d is None. Skipping this calculation."
            )
            return False

        await async_logger(self, "Calculating hourly data for energy averages...")

        for h in range(24):
            hour_start = h
            hour_end = (h + 1) % 24

            # Construct unique_ids for the 3d, 7d, and 14d sensors
            unique_id_1d = get_energy_average_sensor_unique_id(hour_start, hour_end, 1)
            unique_id_3d = get_energy_average_sensor_unique_id(hour_start, hour_end, 3)
            unique_id_7d = get_energy_average_sensor_unique_id(hour_start, hour_end, 7)
            unique_id_14d = get_energy_average_sensor_unique_id(
                hour_start, hour_end, 14
            )

            # Resolve entity_ids for 1d, 3d, 7d, and 14d sensors
            if unique_id_1d not in self._hsem_avg_house_consumption_entity_id_cache:
                entity_id_1d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_1d
                )
                if entity_id_1d is not None:
                    self._hsem_avg_house_consumption_entity_id_cache[unique_id_1d] = (
                        entity_id_1d
                    )
            else:
                entity_id_1d = self._hsem_avg_house_consumption_entity_id_cache[
                    unique_id_1d
                ]

            if unique_id_3d not in self._hsem_avg_house_consumption_entity_id_cache:
                entity_id_3d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_3d
                )
                if entity_id_3d is not None:
                    self._hsem_avg_house_consumption_entity_id_cache[unique_id_3d] = (
                        entity_id_3d
                    )
            else:
                entity_id_3d = self._hsem_avg_house_consumption_entity_id_cache[
                    unique_id_3d
                ]

            if unique_id_7d not in self._hsem_avg_house_consumption_entity_id_cache:
                entity_id_7d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_7d
                )

                if entity_id_7d is not None:
                    self._hsem_avg_house_consumption_entity_id_cache[unique_id_7d] = (
                        entity_id_7d
                    )
            else:
                entity_id_7d = self._hsem_avg_house_consumption_entity_id_cache[
                    unique_id_7d
                ]

            if unique_id_14d not in self._hsem_avg_house_consumption_entity_id_cache:
                entity_id_14d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_14d
                )

                if entity_id_14d is not None:
                    self._hsem_avg_house_consumption_entity_id_cache[unique_id_14d] = (
                        entity_id_14d
                    )
            else:
                entity_id_14d = self._hsem_avg_house_consumption_entity_id_cache[
                    unique_id_14d
                ]

            if (
                entity_id_1d is None
                or entity_id_3d is None
                or entity_id_7d is None
                or entity_id_14d is None
            ):
                self._missing_input_entities_list.append(str(unique_id_1d))
                self._missing_input_entities_list.append(str(unique_id_3d))
                self._missing_input_entities_list.append(str(unique_id_7d))
                self._missing_input_entities_list.append(str(unique_id_14d))
                await async_logger(
                    self,
                    f"One of the required sensors for average house consumptions load is not ready/found. Waiting for next update.",
                )
                return False

            # Default values for sensors in case they are missing
            value_1d = None
            value_3d = None
            value_7d = None
            value_14d = None
            weighted_value_1d = None
            weighted_value_3d = None
            weighted_value_7d = None
            weighted_value_14d = None
            avg_house_consumption = 0.0

            # Fetch values for 1d, 3d, 7d, and 14d if available
            try:
                if entity_id_1d is not None:
                    value_1d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_1d, "float", 3)
                    )
                if entity_id_3d is not None:
                    value_3d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_3d, "float", 3)
                    )

                if entity_id_7d is not None:
                    value_7d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_7d, "float", 3)
                    )

                if entity_id_14d is not None:
                    value_14d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_14d, "float", 3)
                    )
            except ValueError:
                value_1d = None
                value_3d = None
                value_7d = None
                value_14d = None
                avg_house_consumption = 0.0

            if (
                value_1d is None
                or value_3d is None
                or value_7d is None
                or value_14d is None
            ):
                self._missing_input_entities_list.append(str(entity_id_1d))
                self._missing_input_entities_list.append(str(entity_id_3d))
                self._missing_input_entities_list.append(str(entity_id_7d))
                self._missing_input_entities_list.append(str(entity_id_14d))
                await async_logger(
                    self,
                    f"One of the required sensors for average house consumptions load is not ready/found. Waiting for next update.",
                )
                return False

            # Read configured weights (percent)
            w1 = int(self._hsem_house_consumption_energy_weight_1d)
            w3 = int(self._hsem_house_consumption_energy_weight_3d)
            w7 = int(self._hsem_house_consumption_energy_weight_7d)
            w14 = int(self._hsem_house_consumption_energy_weight_14d)
            w_total_config = w1 + w3 + w7 + w14

            if w_total_config == 0:
                await async_logger(self, "All weights sum to 0. Skipping calculation.")
                continue

            # --- Mild capping between 7d and 14d (fail-safe) ---
            value_7d_eff = max(
                CAP7_DOWN * value_14d, min(value_7d, CAP7_UP * value_14d)
            )
            value_14d_eff = max(
                CAP14_DOWN * value_7d_eff, min(value_14d, CAP14_UP * value_7d_eff)
            )

            # --- Baseline and capping for 1d/3d vs calm baseline ---
            baseline = (
                BASELINE_7D_SHARE * value_7d_eff + BASELINE_14D_SHARE * value_14d_eff
            )

            lower1 = baseline * CHANGE_LIMIT_DOWN_FACTOR
            upper1 = baseline * CHANGE_LIMIT_UP_FACTOR
            value_1d_eff = max(lower1, min(value_1d, upper1))

            lower3 = baseline * CHANGE3_LIMIT_DOWN_FACTOR
            upper3 = baseline * CHANGE3_LIMIT_UP_FACTOR
            value_3d_eff = max(lower3, min(value_3d, upper3))

            # --- Spike detection severities (0..1) ---
            ratio1 = (
                (value_1d / value_7d_eff)
                if (value_7d_eff and value_7d_eff > 0)
                else 1.0
            )
            if ratio1 <= SPIKE1_RATIO_MIN:
                sev1 = 0.0
            elif ratio1 >= SPIKE1_RATIO_MAX:
                sev1 = 1.0
            else:
                sev1 = (ratio1 - SPIKE1_RATIO_MIN) / (
                    SPIKE1_RATIO_MAX - SPIKE1_RATIO_MIN
                )

            ratio3 = (
                (value_3d / value_7d_eff)
                if (value_7d_eff and value_7d_eff > 0)
                else 1.0
            )
            if ratio3 <= SPIKE3_RATIO_MIN:
                sev3 = 0.0
            elif ratio3 >= SPIKE3_RATIO_MAX:
                sev3 = 1.0
            else:
                sev3 = (ratio3 - SPIKE3_RATIO_MIN) / (
                    SPIKE3_RATIO_MAX - SPIKE3_RATIO_MIN
                )

            ratio7 = (
                (value_7d_eff / value_14d_eff)
                if (value_14d_eff and value_14d_eff > 0)
                else 1.0
            )
            if ratio7 <= SPIKE7_RATIO_MIN:
                sev7 = 0.0
            elif ratio7 >= SPIKE7_RATIO_MAX:
                sev7 = 1.0
            else:
                sev7 = (ratio7 - SPIKE7_RATIO_MIN) / (
                    SPIKE7_RATIO_MAX - SPIKE7_RATIO_MIN
                )

            ratio14 = (
                (value_14d_eff / value_7d_eff)
                if (value_7d_eff and value_7d_eff > 0)
                else 1.0
            )
            if ratio14 <= SPIKE14_RATIO_MIN:
                sev14 = 0.0
            elif ratio14 >= SPIKE14_RATIO_MAX:
                sev14 = 1.0
            else:
                sev14 = (ratio14 - SPIKE14_RATIO_MIN) / (
                    SPIKE14_RATIO_MAX - SPIKE14_RATIO_MIN
                )

            # --- Dynamic reweighting (all relative to configured weights) ---
            # 1d → redistribute to 3d/7d/14d
            freed1 = w1 * (SPIKE1_REDUCE_FRACTION_MAX * sev1)
            w1_eff = w1 - freed1
            w3_eff = w3 + freed1 * SPIKE1_REDIST_TO_3D
            w7_eff = w7 + freed1 * SPIKE1_REDIST_TO_7D
            w14_eff = w14 + freed1 * SPIKE1_REDIST_TO_14D

            # 3d → redistribute to 7d/14d
            freed3 = w3_eff * (SPIKE3_REDUCE_FRACTION_MAX * sev3)
            w3_eff = w3_eff - freed3
            w7_eff = w7_eff + freed3 * SPIKE3_REDIST_TO_7D
            w14_eff = w14_eff + freed3 * SPIKE3_REDIST_TO_14D

            # 7d too high vs 14d → redistribute a little to 14d
            freed7 = w7_eff * (SPIKE7_REDUCE_FRACTION_MAX * sev7)
            w7_eff = w7_eff - freed7
            w14_eff = w14_eff + freed7 * SPIKE7_REDIST_TO_14D

            # 14d too high vs 7d → redistribute a little to 7d
            freed14 = w14_eff * (SPIKE14_REDUCE_FRACTION_MAX * sev14)
            w14_eff = w14_eff - freed14
            w7_eff = w7_eff + freed14 * SPIKE14_REDIST_TO_7D

            # --- Reliability-based scaling (down-weight disagreement), then renormalize ---
            rel1 = 1.0 / (RELIABILITY_EPS + abs(value_1d_eff - value_7d_eff))
            rel3 = 1.0 / (RELIABILITY_EPS + abs(value_3d_eff - value_7d_eff))
            rel7 = 1.0 / (RELIABILITY_EPS + abs(value_7d_eff - value_14d_eff))
            rel14 = 1.0 / (RELIABILITY_EPS + abs(value_14d_eff - value_7d_eff))

            rel1 = 1.0 + (rel1 - 1.0) * RELIABILITY_SCALE_STRENGTH
            rel3 = 1.0 + (rel3 - 1.0) * RELIABILITY_SCALE_STRENGTH
            rel7 = 1.0 + (rel7 - 1.0) * RELIABILITY_SCALE_STRENGTH
            rel14 = 1.0 + (rel14 - 1.0) * RELIABILITY_SCALE_STRENGTH

            w1_eff *= rel1
            w3_eff *= rel3
            w7_eff *= rel7
            w14_eff *= rel14

            w_sum_eff = w1_eff + w3_eff + w7_eff + w14_eff
            if w_sum_eff > 0:
                scale_back = w_total_config / w_sum_eff
                w1_eff *= scale_back
                w3_eff *= scale_back
                w7_eff *= scale_back
                w14_eff *= scale_back
            else:
                # nothing to weight with; keep configured weights
                w1_eff, w3_eff, w7_eff, w14_eff = w1, w3, w7, w14

            # Weighted sum (percent → factor); note capped short windows
            weighted_value_1d = value_1d_eff * (w1_eff / 100)
            weighted_value_3d = value_3d_eff * (w3_eff / 100)
            weighted_value_7d = value_7d_eff * (w7_eff / 100)
            weighted_value_14d = value_14d_eff * (w14_eff / 100)

            avg_house_consumption = round(
                (
                    weighted_value_1d
                    + weighted_value_3d
                    + weighted_value_7d
                    + weighted_value_14d
                ),
                3,
            )

            for obj in self._hourly_recommendations:
                # if obj.start.hour == hour_start and obj.end.hour == hour_end:
                if int(obj.start.hour) == int(hour_start):
                    # obj.avg_house_consumption = round(avg_house_consumption, 3)
                    interval_consumption = avg_house_consumption / (
                        60 / self._recommendation_interval_minutes
                    )
                    obj.avg_house_consumption = round(interval_consumption, 3)

        return True

    async def _async_calculate_batteries_schedules(self, start_time=None) -> None:
        """
        Calculate and update the batteries schedules based on the current configuration.

        This method updates each schedule in `self._batteries_schedules` with calculated values for
        `needed_batteries_capacity`, `needed_batteries_capacity_cost`, and `avg_import_price`.
        It also sets the `recommendation` attribute for relevant intervals in `self._hourly_recommendations`.
        """
        now = start_time or datetime.now().astimezone(self._tz)

        for schedule in self._batteries_schedules:

            if not schedule.enabled:
                continue

            needed_batteries_capacity = 0.0
            needed_batteries_capacity_cost = 0.0
            avg_import_price = 0.0
            count = 0

            for recommendation in self._hourly_recommendations:
                if (
                    recommendation.start.date() < now.date()
                    or recommendation.end.date() > now.date()
                ):
                    continue

                r_start = recommendation.start.time()
                r_end = recommendation.end.time()

                if r_start >= schedule.start and r_end <= schedule.end:
                    recommendation.recommendation = (
                        Recommendations.BatteriesDischargeMode.value
                    )

                    avg_import_price += recommendation.import_price
                    needed_batteries_capacity += (
                        recommendation.estimated_net_consumption
                    )
                    needed_batteries_capacity_cost += (
                        recommendation.estimated_net_consumption
                        * recommendation.import_price
                    )
                    count += 1

            schedule.needed_batteries_capacity = round(needed_batteries_capacity, 3)
            schedule.needed_batteries_capacity_cost = round(
                needed_batteries_capacity_cost, 3
            )
            schedule.avg_import_price = (
                round(avg_import_price / count, 3) if count > 0 else 0.0
            )

            await async_logger(
                self,
                f"Enabling batteries discharging schedule for {now}. "
                f"Start: {schedule.start}, "
                f"End: {schedule.end}, "
                f"Average Import Price: {round(avg_import_price, 2)}, "
                f"Needed Batteries Capacity: {round(needed_batteries_capacity, 2)} kWh, "
                f"Needed Batteries Capacity Cost: {round(needed_batteries_capacity_cost, 2)}, ",
            )

    async def _async_calculate_batteries_schedules_best_charge_time(
        self, start_time=None
    ) -> None:
        """
        Calculate the best times to charge batteries based on active schedules.
        Identifies the cheapest charging times to meet the combined energy needs of all schedules,
        while respecting battery capacity and current charge.
        """
        # now = datetime.now().astimezone(self._tz)
        now = start_time or datetime.now().astimezone(self._tz)

        await async_logger(
            self,
            f"Calculating best time to charge batteries based on active schedules at {now} ",
        )

        # if self._hsem_huawei_solar_batteries_state_of_capacity_state == 100:
        #    await async_logger(
        #        self,
        #        "Skipping charge as the batteries are already at 100% capacity. ",
        #    )
        #    return

        # Gather all active schedules
        schedules = []

        # Filter schedules that are still relevant
        schedules = [
            s
            for s in self._batteries_schedules
            if s.start > now.time() and now.time() < s.end and s.enabled
        ]
        await async_logger(
            self,
            f"{len(schedules)} batteries schedules remain after filtering based on the current time and if they are enabled.",
        )

        if not schedules:
            return

        # Sort the schedules by start time
        schedules.sort(key=lambda s: s.start)

        # Calculate the total required charge across all schedules
        total_required_charge = sum(s.needed_batteries_capacity for s in schedules)
        first_schedule = datetime.combine(now, schedules[0].start).astimezone(self._tz)
        item = next(
            (r for r in self._hourly_recommendations if r.start == first_schedule), None
        )

        if item is not None:
            await async_logger(
                self,
                f"Total Required kWh To Charge Battery: {round(total_required_charge, 2)} kWh. "
                f"Expected battery capacity before the scheduled start: {item.estimated_battery_capacity} kWh.",
            )

            if item.estimated_battery_capacity >= total_required_charge:
                await async_logger(
                    self,
                    "Skipping charge as the batteries already have sufficient capacity before the first schedule starts.",
                )
                return

        # Find the best time to charge before the first schedules starts
        self._batteries_schedules_remaining_capacity_needed = 0.00
        for schedule in schedules:
            self._batteries_schedules_remaining_capacity_needed += round(
                schedule.needed_batteries_capacity, 2
            )
            await async_logger(
                self,
                f"Finding the best time to charge {round(schedule.needed_batteries_capacity, 2)} before {schedule.start} to cover battery schedule.",
            )

            await self._async_find_best_time_to_charge_battery_schedule(
                schedule, start_time
            )

    async def _async_find_best_time_to_charge_battery_schedule(
        self, battery_schedule: BatterySchedule, start_time=None
    ) -> None:
        """Find best time to charge based on prioritized conditions."""
        now = start_time or datetime.now().astimezone(self._tz)

        # Calculate max charge per hour with conversion loss
        conversion_loss_factor = 1 - (
            convert_to_float(self._hsem_batteries_conversion_loss) / 100
        )
        max_charge_per_hour = (
            convert_to_float(
                self._hsem_huawei_solar_batteries_maximum_charging_power_state
            )
            / 1000
        ) * conversion_loss_factor

        # Adjust max charge per hour based on recommendation interval
        max_charge_per_interval = max_charge_per_hour / (
            60 / self._recommendation_interval_minutes
        )

        if max_charge_per_interval <= 0:
            await async_logger(
                self, "Invalid maximum charging power. Skipping calculation."
            )
            return

        # await async_logger(
        #    self,
        #    f"Planning of charging the batteries started. "
        #    f"Time range: {battery_schedule.start.time()} and {battery_schedule.end.time()}. "
        #    f"Max batteries capacity: {round(self._hsem_batteries_rated_capacity_max_state / 1000, 2)} kWh. "
        #    f"Conversion loss: {round(self._hsem_batteries_conversion_loss, 2)}%. "
        #    f"Max charge per hour: {round(max_charge_per_hour, 2)} kWh. ",
        # )

        # Only allow recommendations before the charging schedule starts
        # filtered_hourly_recommendations = [
        #     rec
        #     for rec in self._hourly_recommendations
        #     if rec.start.date() == now.date()
        #     and rec.end.time() < battery_schedule.start.time()
        #     and rec.start.time() >= now.time()
        #     and rec.recommendation is None
        # ]
        filtered_hourly_recommendations = [
            rec
            for rec in self._hourly_recommendations
            if rec.start.date() == now.date()
            and rec.end.time() < battery_schedule.start
            and rec.end.time() > now.time()
            and rec.recommendation is None
        ]

        # First priority: Negative import prices
        sorted_filtered = [
            rec for rec in filtered_hourly_recommendations if rec.import_price < 0
        ]

        # Sort based on negative import price.
        sorted_filtered.sort(key=lambda x: x.import_price)

        charged_energy = 0.0

        for rec in sorted_filtered:
            if (
                rec.import_price >= 0
                or charged_energy >= battery_schedule.needed_batteries_capacity
            ):
                break

            energy_to_charge = min(
                max_charge_per_interval,
                battery_schedule.needed_batteries_capacity - charged_energy,
            )

            if energy_to_charge > 0:
                rec.recommendation = Recommendations.BatteriesChargeGrid.value
                rec.batteries_charged = round(energy_to_charge, 3)
                charged_energy += energy_to_charge

                await async_logger(
                    self,
                    f"Charge interval: {rec.start.date()} {rec.start.time()} - {rec.end.time()}. "
                    f"Charging from grid due to negative import price. "
                    f"Import Price: {rec.import_price}"
                    f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                    f"Total energy charged: {round(charged_energy, 2)} kWh. ",
                )

        if charged_energy >= battery_schedule.needed_batteries_capacity:
            await async_logger(
                self,
                f"Charging complete. Total energy charged: {round(charged_energy, 2)} kWh. ",
            )
            return
        else:
            await async_logger(
                self,
                f"Charged energy after negative price consideration: {round(charged_energy, 2)} kWh. "
                f"Still need to charge: {round(battery_schedule.needed_batteries_capacity - charged_energy, 2)} kWh. ",
            )

        # Second priority: Solar surplus
        if charged_energy < battery_schedule.needed_batteries_capacity:
            sorted_filtered = [
                rec
                for rec in filtered_hourly_recommendations
                if rec.estimated_net_consumption < -0.2
                and rec.recommendation
                is None  # TODO: 0.2 -> in config. This is 200w buffer.
            ]
            sorted_filtered.sort(key=lambda x: x.estimated_net_consumption)

            for rec in sorted_filtered:
                if charged_energy >= battery_schedule.needed_batteries_capacity:
                    break

                available_solar = abs(rec.estimated_net_consumption)
                energy_to_charge = min(
                    max_charge_per_interval,
                    battery_schedule.needed_batteries_capacity - charged_energy,
                    available_solar,
                )

                if energy_to_charge > 0:
                    rec.recommendation = Recommendations.BatteriesChargeSolar.value
                    rec.batteries_charged = round(energy_to_charge, 3)
                    charged_energy += energy_to_charge

                    await async_logger(
                        self,
                        f"Charge interval: {rec.start.date()} {rec.start.time()} - {rec.end.time()}. "
                        f"Charging from solar. "
                        f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                        f"Total energy charged: {round(charged_energy, 2)} kWh. "
                        f"Available Solar: {round(available_solar, 2)} kWh. "
                        f"Net Consumption: {round(rec.estimated_net_consumption, 2)} kWh. ",
                    )

        if charged_energy >= battery_schedule.needed_batteries_capacity:
            await async_logger(
                self,
                f"Charging complete. Total energy charged: {round(charged_energy, 2)} kWh. ",
            )
            return
        else:
            await async_logger(
                self,
                f"Charged energy after solar surplus consideration: {round(charged_energy, 2)} kWh. "
                f"Still need to charge: {round(battery_schedule.needed_batteries_capacity - charged_energy, 2)} kWh. ",
            )

        # Third priority: Cheapest remaining hours considering partial solar contribution
        charged_energy_before = charged_energy
        min_price_check = True

        # Calculate average import price of the schedule
        # and average import price of the charging intervals
        if charged_energy < battery_schedule.needed_batteries_capacity:
            sorted_filtered = [
                rec
                for rec in filtered_hourly_recommendations
                if rec.recommendation is None
            ]
            sorted_filtered.sort(key=lambda x: x.import_price)

            avg_charge_import_price = 0.0
            avg_charge_import_count = 0

            for rec in sorted_filtered:
                if charged_energy >= battery_schedule.needed_batteries_capacity:
                    break

                available_solar = (
                    abs(rec.estimated_net_consumption)
                    if rec.estimated_net_consumption < 0
                    else 0
                )
                grid_energy_needed = min(
                    max_charge_per_interval - available_solar,
                    battery_schedule.needed_batteries_capacity
                    - charged_energy
                    - available_solar,
                )

                energy_to_charge = available_solar + grid_energy_needed

                if energy_to_charge > 0:
                    avg_charge_import_count += 1
                    avg_charge_import_price += rec.import_price
                    charged_energy += energy_to_charge

                    # await async_logger(
                    #     self,
                    #     f"Interval: {rec.start.date()} {rec.start.time()} - {rec.end.time()}. Import Price: {round(rec.import_price, 3)}. Energy Charged: {round(energy_to_charge, 2)} kWh. Total energy charged: {round(charged_energy, 2)} kWh. ",
                    # )

            avg_charge_import = (
                avg_charge_import_price / avg_charge_import_count
                if avg_charge_import_count > 0
                else avg_charge_import_price
            )
            avg_charge_diff = battery_schedule.avg_import_price - avg_charge_import

            if (
                battery_schedule.min_price_difference_required != 0
                and avg_charge_diff < battery_schedule.min_price_difference_required
            ):
                min_price_check = False

            if avg_charge_import_count > 0:
                await async_logger(
                    self,
                    f"Charging from grid average cost calculation. "
                    f"Average Charge Import Price: {round(avg_charge_import, 2)}, "
                    f"Average Usage Price: {round(battery_schedule.avg_import_price, 2)}, "
                    f"Charge Price Difference: {round(avg_charge_diff, 2)}, "
                    f"Min Price Difference: {round(battery_schedule.min_price_difference_required, 2)}, ",
                )

        if min_price_check:
            await async_logger(
                self,
                f"Minimum price difference condition met. Proceeding with grid charging.",
            )
        else:
            await async_logger(
                self,
                f"Minimum price difference condition NOT met. Skipping grid charging.",
            )

        # Reset charged energy to the value before the avg calculation and actually apply the recommendations
        charged_energy = charged_energy_before

        if (
            charged_energy < battery_schedule.needed_batteries_capacity
            and min_price_check
        ):
            sorted_filtered = [
                rec
                for rec in filtered_hourly_recommendations
                if rec.recommendation == None
            ]
            sorted_filtered.sort(key=lambda x: x.import_price)

            for rec in sorted_filtered:
                if charged_energy >= battery_schedule.needed_batteries_capacity:
                    break

                available_solar = (
                    abs(rec.estimated_net_consumption)
                    if rec.estimated_net_consumption < 0
                    else 0
                )
                grid_energy_needed = min(
                    max_charge_per_interval - available_solar,
                    battery_schedule.needed_batteries_capacity
                    - charged_energy
                    - available_solar,
                )

                energy_to_charge = available_solar + grid_energy_needed

                if energy_to_charge > 0:
                    rec.recommendation = Recommendations.BatteriesChargeGrid.value
                    rec.batteries_charged = round(energy_to_charge, 3)
                    charged_energy += energy_to_charge

                    await async_logger(
                        self,
                        f"Charge interval: {rec.start.date()} {rec.start.time()} - {rec.end.time()}. "
                        f"Charging from grid. "
                        f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                        f"Total energy charged: {round(charged_energy, 2)} kWh. "
                        f"Available Solar: {round(available_solar, 2)} kWh. "
                        f"Net Consumption: {round(rec.estimated_net_consumption, 2)} kWh. "
                        f"Import Price: {rec.import_price}",
                    )

        await async_logger(
            self,
            f"Planning of charging the batteries completed for schedule "
            f"({battery_schedule.start} - {battery_schedule.end}, "
            f"Needed Capacity: {round(battery_schedule.needed_batteries_capacity, 2)} kWh). "
            f"Total energy charged: {round(charged_energy, 2)} kWh. ",
        )

    async def _async_optimization_strategy(self) -> None:
        """Calculate the optimization strategy for each hour of the day."""

        now = datetime.now().astimezone(self._tz)
        current_month = now.month

        await async_logger(
            self, "Starting optimization strategy for all remaining hours."
        )

        for rec in self._hourly_recommendations:
            if (
                rec.export_price > rec.import_price
                # and rec.start.date() == now.date()
                and rec.recommendation is None
            ):
                rec.recommendation = Recommendations.ForceExport.value
                await async_logger(
                    self,
                    f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Recommendation set to Force Export (export price > import price).",
                )

        # Calculate when to charge the battery to the full from solar
        batteries_needed_charge = (
            self._hsem_batteries_usable_capacity - self._hsem_batteries_current_capacity
        )

        if batteries_needed_charge < 0.0:
            batteries_needed_charge = 0.0

        await async_logger(
            self,
            f"Batteries needed charge: {round(batteries_needed_charge,2)} kWh | Current capacity: {self._hsem_batteries_current_capacity} kWh | Usable capacity: {self._hsem_batteries_usable_capacity} kWh",
        )

        charged = 0.0

        # Loop through hourly_calculations sorted by export_price to charge batteries from solar while import price is highest
        self._hourly_recommendations.sort(key=lambda x: x.export_price)
        for rec in self._hourly_recommendations:
            if rec.recommendation is not None:
                continue

            if rec.start.date() != now.date():
                continue

            if charged >= batteries_needed_charge:
                break

            # Negative net consumption means we have solar surplus to charge batteries while covering the house
            if rec.estimated_net_consumption < 0:
                charged += (
                    rec.estimated_net_consumption * -1
                )  # Convert negative to positive for charging
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
                await async_logger(
                    self,
                    f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Charging from solar surplus. Net Consumption: {rec.estimated_net_consumption} | Import Price: {rec.import_price} | Export Price: {rec.export_price} | Total charged: {round(charged, 3)} kWh.",
                )

        # So did we charge the battery totally?
        fully_charged_battery = False
        if (
            charged >= batteries_needed_charge
            or self._hsem_batteries_current_capacity
            == self._hsem_batteries_usable_capacity
        ):
            fully_charged_battery = True

        await async_logger(
            self,
            f"Fully charged battery: {fully_charged_battery}, Total charged from PV: {round(charged, 2)} kWh. Usable capacity: {self._hsem_batteries_usable_capacity} kWh.",
        )

        for rec in self._hourly_recommendations:
            if rec.recommendation is not None:
                await async_logger(
                    self,
                    f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Recommendation already set to {rec.recommendation }. Skipping.",
                )
                continue

            if str(current_month) in str(self._hsem_months_winter):
                rec.recommendation = Recommendations.BatteriesWaitMode.value
                await async_logger(
                    self,
                    f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Winter/Spring: Setting recommendation to BatteriesWaitMode.",
                )

            if str(current_month) in str(self._hsem_months_summer):
                if rec.estimated_net_consumption <= 0.1:
                    rec.recommendation = Recommendations.BatteriesChargeSolar.value
                    await async_logger(
                        self,
                        f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Summer: solar estimate: {round(rec.solcast_pv_estimate, 3)} kWh, setting recommendation to BatteriesChargeSolar. | Import Price: {rec.import_price} | Export Price: {rec.export_price} | Net Consumption: {rec.estimated_net_consumption}",
                    )
                else:
                    rec.recommendation = Recommendations.BatteriesDischargeMode.value

                    await async_logger(
                        self,
                        f"Interval: {rec.start.date()} {rec.start.time()} {rec.end.time()} | Summer: no solar estimate, setting recommendation to BatteriesDischargeMode. | Import Price: {rec.import_price} | Export Price: {rec.export_price} | Net Consumption: {rec.estimated_net_consumption}",
                    )

        await async_logger(
            self, "Completed optimization strategy for all remaining hours."
        )

    def _update_settings(self) -> None:
        """Fetch updated settings from config_entry options."""
        self._read_only = get_config_value(self._config_entry, "hsem_read_only")

        self._hsem_months_winter = get_config_value(
            self._config_entry, "hsem_months_winter"
        )

        self._hsem_months_summer = get_config_value(
            self._config_entry, "hsem_months_summer"
        )

        if not isinstance(self._hsem_months_winter, list):
            self._hsem_months_winter = []

        if not isinstance(self._hsem_months_summer, list):
            self._hsem_months_summer = []

        self._hsem_extended_attributes = get_config_value(
            self._config_entry, "hsem_extended_attributes"
        )

        self._hsem_verbose_logging = get_config_value(
            self._config_entry, "hsem_verbose_logging"
        )
        self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou = (
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
            )
        )
        self._update_interval = convert_to_int(
            get_config_value(self._config_entry, "hsem_update_interval")
        )

        self._hsem_huawei_solar_device_id_inverter_1 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_1"
        )

        self._hsem_huawei_solar_device_id_inverter_2 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_2"
        )
        self._hsem_huawei_solar_device_id_batteries = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_batteries"
        )
        self._hsem_huawei_solar_batteries_working_mode = get_config_value(
            self._config_entry, "hsem_huawei_solar_batteries_working_mode"
        )
        self._hsem_huawei_solar_batteries_state_of_capacity = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_state_of_capacity",
        )
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry,
            "hsem_house_consumption_power",
        )
        self._hsem_solar_production_power = get_config_value(
            self._config_entry,
            "hsem_solar_production_power",
        )
        self._hsem_ev_charger_status = get_config_value(
            self._config_entry, "hsem_ev_charger_status"
        )

        if self._hsem_ev_charger_status == vol.UNDEFINED:
            self._hsem_ev_charger_status = None

        self._hsem_solcast_pv_forecast_forecast_today = get_config_value(
            self._config_entry,
            "hsem_solcast_pv_forecast_forecast_today",
        )
        self._hsem_energi_data_service_import = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_import",
        )
        self._hsem_energi_data_service_export = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_export",
        )
        self._hsem_energi_data_service_export_min_price = convert_to_float(
            get_config_value(
                self._config_entry,
                "hsem_energi_data_service_export_min_price",
            )
        )
        self._hsem_huawei_solar_inverter_active_power_control = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_inverter_active_power_control",
        )
        self._hsem_house_power_includes_ev_charger_power = convert_to_boolean(
            get_config_value(
                self._config_entry,
                "hsem_house_power_includes_ev_charger_power",
            )
        )
        self._hsem_solcast_pv_forecast_forecast_likelihood = get_config_value(
            self._config_entry,
            "hsem_solcast_pv_forecast_forecast_likelihood",
        )
        self._hsem_ev_charger_force_max_discharge_power = convert_to_boolean(
            get_config_value(
                self._config_entry,
                "hsem_ev_charger_force_max_discharge_power",
            )
        )
        self._hsem_ev_charger_max_discharge_power = convert_to_int(
            get_config_value(
                self._config_entry,
                "hsem_ev_charger_max_discharge_power",
            )
        )

        self._hsem_ev_charger_power = get_config_value(
            self._config_entry,
            "hsem_ev_charger_power",
        )

        if self._hsem_ev_charger_power == vol.UNDEFINED:
            self._hsem_ev_charger_power = None

        self._hsem_batteries_conversion_loss = convert_to_float(
            get_config_value(
                self._config_entry,
                "hsem_batteries_conversion_loss",
            )
        )

        self._hsem_huawei_solar_batteries_maximum_charging_power = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_maximum_charging_power",
        )
        self._hsem_huawei_solar_batteries_maximum_discharging_power = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_maximum_discharging_power",
        )
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
        )
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods = (
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
            )
        )
        self._hsem_house_consumption_energy_weight_1d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_1d"
            )
        )
        self._hsem_house_consumption_energy_weight_3d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_3d"
            )
        )
        self._hsem_house_consumption_energy_weight_7d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_7d"
            )
        )
        self._hsem_house_consumption_energy_weight_14d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_14d"
            )
        )
        self._hsem_batteries_rated_capacity_max = get_config_value(
            self._config_entry, "hsem_huawei_solar_batteries_rated_capacity"
        )
        self._hsem_batteries_enable_batteries_schedule_1 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1"
        )
        self._hsem_batteries_enable_batteries_schedule_1_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1_start"
        )
        self._hsem_batteries_enable_batteries_schedule_1_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1_end"
        )
        self._hsem_batteries_enable_batteries_schedule_1_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
            )
        )
        self._hsem_batteries_enable_batteries_schedule_2 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2"
        )
        self._hsem_batteries_enable_batteries_schedule_2_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2_start"
        )
        self._hsem_batteries_enable_batteries_schedule_2_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2_end"
        )
        self._hsem_batteries_enable_batteries_schedule_2_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
            )
        )
        self._hsem_batteries_enable_batteries_schedule_3 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3"
        )
        self._hsem_batteries_enable_batteries_schedule_3_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3_start"
        )
        self._hsem_batteries_enable_batteries_schedule_3_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3_end"
        )
        self._hsem_batteries_enable_batteries_schedule_3_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
            )
        )

        if self._hsem_huawei_solar_device_id_inverter_2 is not None:
            if len(self._hsem_huawei_solar_device_id_inverter_2) == 0:
                self._hsem_huawei_solar_device_id_inverter_2 = None

    async def _async_fetch_entity_states(self) -> None:
        # Reset status
        self._missing_input_entities = False
        self._missing_input_entities_list = []

        # Resolve force working mode once
        if self._hsem_force_working_mode is None:
            self._hsem_force_working_mode = (
                await async_resolve_entity_id_from_unique_id(
                    self, "hsem_force_working_mode", "select"
                )
            )

        def _read_entity(entity_id, conv_type=None, decimals=3, label=""):
            """Read entity state safely and track any errors."""
            if not entity_id:
                self._missing_input_entities = True
                self._missing_input_entities_list.append(
                    f"Missing entity: {label or entity_id}"
                )
                return None
            try:
                return ha_get_entity_state_and_convert(
                    self, entity_id, conv_type, decimals
                )
            except Exception as e:
                self._missing_input_entities = True
                self._missing_input_entities_list.append(
                    f"Error reading {label or entity_id}: {type(e).__name__}: {e}"
                )
                return None

        # Read each entity using the helper
        self._hsem_force_working_mode_state = _read_entity(
            self._hsem_force_working_mode, "string", label="hsem_force_working_mode"
        )

        if self._hsem_force_working_mode_state is None:
            self._hsem_force_working_mode_state = "auto"

        if self._hsem_ev_charger_status:
            self._hsem_ev_charger_status_state = convert_to_boolean(
                _read_entity(
                    self._hsem_ev_charger_status,
                    "boolean",
                    label="hsem_ev_charger_status",
                )
            )

        if self._hsem_ev_charger_power:
            self._hsem_ev_charger_power_state = convert_to_float(
                _read_entity(
                    self._hsem_ev_charger_power, "float", label="hsem_ev_charger_power"
                )
            )

        self._hsem_house_consumption_power_state = convert_to_float(
            _read_entity(
                self._hsem_house_consumption_power,
                "float",
                label="hsem_house_consumption_power",
            )
        )
        self._hsem_solar_production_power_state = convert_to_float(
            _read_entity(
                self._hsem_solar_production_power,
                "float",
                label="hsem_solar_production_power",
            )
        )
        self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou_state = (
            _read_entity(
                self._hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou,
                "string",
                label="excess_pv_energy_use_in_tou",
            )
        )
        self._hsem_huawei_solar_batteries_working_mode_state = _read_entity(
            self._hsem_huawei_solar_batteries_working_mode,
            "string",
            label="batteries_working_mode",
        )
        self._hsem_huawei_solar_batteries_state_of_capacity_state = convert_to_float(
            _read_entity(
                self._hsem_huawei_solar_batteries_state_of_capacity,
                "float",
                label="state_of_capacity",
            )
        )
        self._hsem_energi_data_service_import_state = convert_to_float(
            _read_entity(
                self._hsem_energi_data_service_import, "float", 3, label="eds_import"
            )
        )
        self._hsem_energi_data_service_export_state = convert_to_float(
            _read_entity(
                self._hsem_energi_data_service_export, "float", 3, label="eds_export"
            )
        )
        self._hsem_huawei_solar_inverter_active_power_control_state = _read_entity(
            self._hsem_huawei_solar_inverter_active_power_control,
            "string",
            label="inverter_active_power_control",
        )
        self._hsem_huawei_solar_batteries_maximum_charging_power_state = (
            convert_to_float(
                _read_entity(
                    self._hsem_huawei_solar_batteries_maximum_charging_power,
                    "float",
                    label="max_charging_power",
                )
            )
        )
        self._hsem_huawei_solar_batteries_maximum_discharging_power_state = (
            convert_to_float(
                (
                    _read_entity(
                        self._hsem_huawei_solar_batteries_maximum_discharging_power,
                        "float",
                        label="max_discharging_power",
                    )
                )
            )
        )
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state = (
            convert_to_float(
                _read_entity(
                    self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
                    "float",
                    label="grid_charge_cutoff_soc",
                )
            )
        )
        self._hsem_batteries_rated_capacity_max_state = convert_to_float(
            _read_entity(
                self._hsem_batteries_rated_capacity_max,
                "float",
                label="batteries_rated_capacity_max",
            )
        )

        # Special handling for TOU charging/discharging periods
        if self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods:
            try:
                entity_data = ha_get_entity_state_and_convert(
                    self,
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
                    None,
                )

                # Reset both values first
                self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
                    None
                )
                self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = (
                    None
                )

                if isinstance(entity_data, State):
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
                        entity_data.state
                    )
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = [
                        entity_data.attributes[f"Period {i}"]
                        for i in range(1, 11)
                        if f"Period {i}" in entity_data.attributes
                    ]
                else:
                    self._missing_input_entities = True
                    self._missing_input_entities_list.append(
                        "TOU periods entity is not of type State"
                    )
            except Exception as e:
                self._missing_input_entities = True
                self._missing_input_entities_list.append(
                    f"Error reading TOU periods: {type(e).__name__}: {e}"
                )
        else:
            self._missing_input_entities = True
            self._missing_input_entities_list.append("Missing entity: TOU periods")

        # Register state listeners (unchanged)
        if (
            self._hsem_ev_charger_status
            and self._hsem_ev_charger_status not in self._tracked_entities
        ):
            await async_logger(
                self,
                f"Starting to track state changes for {self._hsem_ev_charger_status}",
            )
            async_track_state_change_event(
                self.hass, [self._hsem_ev_charger_status], self._async_handle_update
            )
            self._tracked_entities.add(self._hsem_ev_charger_status)

        if (
            self._hsem_force_working_mode
            and self._hsem_force_working_mode not in self._tracked_entities
        ):
            await async_logger(
                self,
                f"Starting to track state changes for {self._hsem_force_working_mode}",
            )
            async_track_state_change_event(
                self.hass, [self._hsem_force_working_mode], self._async_handle_update
            )
            self._tracked_entities.add(self._hsem_force_working_mode)

    async def _async_calculate_remaining_battery_capacity(self) -> None:
        # Calculate remaining battery capacity and max allowed charge from grid if all necessary values are available
        if isinstance(
            self._hsem_batteries_rated_capacity_max_state, (int, float)
        ) and isinstance(
            self._hsem_huawei_solar_batteries_state_of_capacity_state, (int, float)
        ):
            self._hsem_batteries_rated_capacity_min_state = (
                self._hsem_batteries_rated_capacity_max_state * 0.05
            )

            # Calculate usable capacity (kWh)
            self._hsem_batteries_usable_capacity = round(
                (self._hsem_batteries_rated_capacity_max_state / 1000),
                2,
            )

            # Calculate current capacity (kWh)
            self._hsem_batteries_current_capacity = round(
                max(
                    0,
                    (
                        self._hsem_huawei_solar_batteries_state_of_capacity_state
                        / 100
                        * (self._hsem_batteries_rated_capacity_max_state / 1000)
                    ),
                ),
                2,
            )

    async def _async_calculate_net_consumption(self) -> None:
        # Calculate the net consumption without the EV charger power

        if isinstance(
            self._hsem_solar_production_power_state, (int, float)
        ) and isinstance(self._hsem_house_consumption_power_state, (int, float)):

            # Treat EV charger power state as 0.0 if it's None
            ev_charger_power_state = (
                self._hsem_ev_charger_power_state
                if isinstance(self._hsem_ev_charger_power_state, (int, float))
                else 0.0
            )

            if self._hsem_house_power_includes_ev_charger_power:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                    - ev_charger_power_state
                )
            else:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                    + ev_charger_power_state
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )

            self._hsem_net_consumption = round(self._hsem_net_consumption, 3)
            self._hsem_net_consumption_with_ev = round(
                self._hsem_net_consumption_with_ev, 3
            )

            await async_logger(
                self,
                f"Net consumption calculated: {self._hsem_net_consumption}, with EV: {self._hsem_net_consumption_with_ev}, hsem_house_power_includes_ev_charger_power: {self._hsem_house_power_includes_ev_charger_power}, hsem_house_consumption_power_state: {self._hsem_house_consumption_power_state}, hsem_solar_production_power_state: {self._hsem_solar_production_power_state}, ev_charger_power_state: {ev_charger_power_state}",
            )
        else:
            self._hsem_net_consumption = 0.0

    async def _async_set_inverter_power_control(self) -> None:
        # Determine the grid export power percentage based on the state

        if not isinstance(self._hsem_energi_data_service_export_state, (int, float)):
            return

        if not isinstance(
            self._hsem_energi_data_service_export_min_price, (int, float)
        ):
            return

        export_power_percentage = (
            100
            if self._hsem_energi_data_service_export_state
            > self._hsem_energi_data_service_export_min_price
            else 0
        )

        # List of inverters to update
        inverters = [
            self._hsem_huawei_solar_device_id_inverter_1,
            self._hsem_huawei_solar_device_id_inverter_2,
        ]

        if (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 100.0%"
            and export_power_percentage != 100
        ) or (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 0.0%"
            and export_power_percentage != 0
        ):
            for inverter_id in inverters:
                if inverter_id is not None:
                    await async_set_grid_export_power_pct(
                        self, inverter_id, export_power_percentage
                    )

    async def _async_setup_batteries_schedules(self) -> None:
        self._batteries_schedules = []

        # Setup schedules
        schedule = BatterySchedule(
            enabled=convert_to_boolean(
                self._hsem_batteries_enable_batteries_schedule_1
            ),
            start=convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_1_start
            ),
            end=convert_to_time(self._hsem_batteries_enable_batteries_schedule_1_end),
            avg_import_price=0.0,
            needed_batteries_capacity=0.0,
            needed_batteries_capacity_cost=0.0,
            min_price_difference_required=convert_to_float(
                self._hsem_batteries_enable_batteries_schedule_1_min_price_difference
            ),
        )
        self._batteries_schedules.append(schedule)

        schedule = BatterySchedule(
            enabled=convert_to_boolean(
                self._hsem_batteries_enable_batteries_schedule_2
            ),
            start=convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_2_start
            ),
            end=convert_to_time(self._hsem_batteries_enable_batteries_schedule_2_end),
            avg_import_price=0.0,
            needed_batteries_capacity=0.0,
            needed_batteries_capacity_cost=0.0,
            min_price_difference_required=convert_to_float(
                self._hsem_batteries_enable_batteries_schedule_2_min_price_difference
            ),
        )
        self._batteries_schedules.append(schedule)

        schedule = BatterySchedule(
            enabled=convert_to_boolean(
                self._hsem_batteries_enable_batteries_schedule_3
            ),
            start=convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_3_start
            ),
            end=convert_to_time(self._hsem_batteries_enable_batteries_schedule_3_end),
            avg_import_price=0.0,
            needed_batteries_capacity=0.0,
            needed_batteries_capacity_cost=0.0,
            min_price_difference_required=convert_to_float(
                self._hsem_batteries_enable_batteries_schedule_3_min_price_difference
            ),
        )
        self._batteries_schedules.append(schedule)
