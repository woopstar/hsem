"""ML-based house consumption prediction for HSEM.

This module provides an alternative to the rolling-average sensor pipeline
(``custom_sensors/avg_sensor.py``, ``house_consumption_power_sensor.py``,
``integration_sensor.py``).  Instead of integrating a power sensor in
real-time and maintaining 96 rolling-average entities, the ML approach:

1. Queries Home Assistant's recorder directly for historical energy data
   from existing grid import/export energy sensors.
2. Computes per-hour consumption deltas from the accumulator history.
3. Builds a simple per-(day_of_week, hour_of_day) weighted moving average
   with exponential time decay.
4. Feeds the predictions into the planner pipeline in place of the
   ``HourlyConsumptionAverage`` values.

The ML mode is enabled/disabled via a config toggle (``hsem_ml_consumption_enabled``).
When disabled, the existing averaging-sensor pipeline continues to run unchanged.

Design goals:
- Minimal dependencies (no NumPy, no scikit-learn).
- ~200 lines of Python, not ~2 000.
- Stateless — predictions are recomputed from recorder history on every planner cycle.
- Interpretable — the model is just a per-DOW-hour weighted average.

See also:
- :mod:`custom_components.hsem.ml.history_reader` — recorder queries.
- :mod:`custom_components.hsem.ml.consumption_predictor` — the prediction model.
- :mod:`custom_components.hsem.ml.populator` — slot population.
"""
