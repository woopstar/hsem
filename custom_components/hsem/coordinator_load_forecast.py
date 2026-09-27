"""House-load forecast population and readiness for the update cycle.

Extracted from ``coordinator_cycle.py`` to keep that module within the
repository's 30 KB / 1000-line limit (issue #1110). Mixed back into
``HSEMDataUpdateCoordinator`` in MRO order, so ``self`` and every attribute
reference are unchanged.

The ML predictor runs first when enabled. Otherwise, or when it fails, the
rolling-average sensors are used. The result is validated by
:func:`~coordinator_helpers.assess_load_forecast`. Hour blocks without a
stored sample are published in ``data_quality`` and logged once per change,
so a stuck forecast always names the hours it is waiting for (issue #1110).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from custom_components.hsem.coordinator_helpers import assess_load_forecast
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    MAX_ESTIMATED_LOAD_HOURS,
    ConsumptionPopulation,
    populate_avg_house_consumption_from_snapshot,
)
from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import async_log


class CoordinatorLoadForecastMixin(CoordinatorSharedState):
    """Populate the house-load forecast and publish its readiness."""

    def _populate_from_avg_sensors(self, cfg: SensorConfig) -> ConsumptionPopulation:
        """Populate the slots from the rolling-average sensors."""
        assert self._snapshot is not None
        population = populate_avg_house_consumption_from_snapshot(
            self._hourly_recommendations,
            self._snapshot,
            cfg,
            self._avg_house_consumption_entity_id_cache,
            entry_id=self._config_entry.entry_id,
        )
        async_log(
            "debug",
            "[avg] populate_avg_house_consumption_from_snapshot returned %s "
            "(missing=%s estimated=%s), cache has %d entries, snapshot has %d "
            "energy_avg values",
            population.ok,
            list(population.missing_hours),
            list(population.estimated_hours),
            len(self._avg_house_consumption_entity_id_cache),
            len(self._snapshot.energy_average_values),
        )
        return population

    async def _populate_consumption(self, cfg: SensorConfig) -> ConsumptionPopulation:
        """Run ML consumption prediction when enabled, else the avg sensors."""
        if not cfg.ml_consumption_enabled:
            return self._populate_from_avg_sensors(cfg)

        from custom_components.hsem.ml.populator import (
            populate_ml_house_consumption,
        )

        # A slow or failing ML populate must never take the whole update
        # cycle down with it: during initial setup this cycle is awaited
        # directly by async_setup_entry (issue #926), so an uncaught
        # exception here would fail the entire config entry and remove
        # every HSEM entity, not just the ML-driven ones. Fall back to
        # the legacy avg-consumption path instead, same as a clean
        # ``consumption_ok=False`` return, and keep any previously
        # trained predictor so the next cycle can retry without losing
        # its cache.
        try:
            ml_ok, self._ml_predictor = await populate_ml_house_consumption(
                self.hass,
                self._hourly_recommendations,
                cfg,
                self._ml_predictor,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async_log(
                "error",
                "[ml] populate_ml_house_consumption raised %s —"
                " falling back to legacy avg sensors for this cycle.",
                exc,
            )
            ml_ok = False
        else:
            async_log(
                "debug",
                "[ml] populate_ml_house_consumption returned %s",
                ml_ok,
            )

        if ml_ok:
            return ConsumptionPopulation(ok=True)
        async_log(
            "debug",
            "[ml] ML consumption failed — falling back to legacy avg sensors.",
        )
        return self._populate_from_avg_sensors(cfg)

    def _log_load_forecast_gaps(self, population: ConsumptionPopulation) -> None:
        """Warn once whenever the set of missing/estimated hours changes."""
        gaps = (population.missing_hours, population.estimated_hours)
        if gaps == getattr(self, "_last_load_forecast_gaps", ((), ())):
            return
        self._last_load_forecast_gaps = gaps
        if population.estimated_hours:
            async_log(
                "warning",
                "[load] No stored consumption sample for hour block(s) %s; "
                "planning with a conservative neighbour estimate until each "
                "block completes once.",
                list(population.estimated_hours),
            )
        elif population.missing_hours:
            async_log(
                "warning",
                "[load] No stored consumption sample for hour block(s) %s; "
                "more than %d hour(s) are missing, so the forecast is not ready.",
                list(population.missing_hours),
                MAX_ESTIMATED_LOAD_HOURS,
            )

    async def _async_populate_load_forecast(
        self, cfg: SensorConfig, live: LiveState, now: datetime
    ) -> bool:
        """Populate the house-load forecast and record its readiness.

        Returns:
            True when the future load profile is safe to optimise.
        """
        population = await self._populate_consumption(cfg)
        self._load_forecast_population = population
        self._log_load_forecast_gaps(population)

        load_readiness = assess_load_forecast(
            self._hourly_recommendations,
            now,
            population_succeeded=population.ok,
            live_house_demand_w=live.house_consumption_power_w,
        )
        consumption_ok = load_readiness.ready
        self._current_load_forecast_signature = load_readiness.signature
        readiness_reason = load_readiness.reason
        previous_reason = getattr(self, "_last_load_forecast_readiness_reason", None)
        if consumption_ok:
            self._data_quality = replace(
                self._data_quality,
                load_forecast_ready=True,
                load_forecast_reason=None,
            )
            if previous_reason is not None:
                async_log(
                    "info",
                    "[load] Forecast recovered (%s); a fresh plan is required.",
                    previous_reason,
                )
        else:
            assert readiness_reason is not None
            self._load_forecast_recovery_replan_pending = True
            self._data_quality = replace(
                self._data_quality,
                load_forecast_ready=False,
                load_forecast_reason=readiness_reason,
            )
            if readiness_reason != previous_reason:
                async_log(
                    "warning",
                    "[load] Forecast is not ready (%s); automatic control will "
                    "publish a strict storage hold.",
                    readiness_reason,
                )
        self._last_load_forecast_readiness_reason = readiness_reason
        return consumption_ok

    def _published_data_quality(self) -> DataQuality:
        """Return the data-quality report with this cycle's load-forecast gaps.

        The planner phase replaces ``_data_quality`` with the planner's own
        report, so the gap lists are attached when the snapshot is published.
        """
        population: ConsumptionPopulation = getattr(
            self, "_load_forecast_population", ConsumptionPopulation(ok=False)
        )
        return replace(
            self._data_quality,
            load_forecast_missing_hours=list(population.missing_hours),
            load_forecast_estimated_hours=list(population.estimated_hours),
        )
