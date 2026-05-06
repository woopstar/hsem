"""
Integration Guide: Adaptive Power Prediction for Working Mode Sensor

This document outlines how to integrate the AdaptiveConsumptionPredictor
into the working_mode_sensor to improve consumption predictions and
recommendation accuracy.
"""

# ============================================================================
# STEP 1: ADD ADAPTIVE PREDICTOR TO WORKING MODE SENSOR
# ============================================================================

# In working_mode_sensor.py, add import:
from custom_components.hsem.custom_sensors.adaptive_consumption_predictor import (
    AdaptiveConsumptionPredictor,
)

# In __init__, initialize the predictor:
class HSEMWorkingModeSensor(SensorEntity, HSEMEntity):
    def __init__(self, config_entry) -> None:
        super().__init__(config_entry)

        # ... existing code ...

        # Initialize adaptive consumption predictor
        # tau_days should be tuned from your historical data (typically 5-10 days)
        self._adaptive_predictor = AdaptiveConsumptionPredictor(
            tau_days=7.0,      # Adjust based on your consumption pattern
            min_samples=3,
            max_days_lookback=60,
        )

        # Cache for consumption predictions (store last result to track trends)
        self._last_adaptive_prediction = None
        self._last_prediction_confidence = 0.0
        self._measurements_history = {}  # date -> consumption (kWh)


# ============================================================================
# STEP 2: COLLECT CONSUMPTION MEASUREMENTS
# ============================================================================

# In _async_handle_update (or wherever you aggregate consumption data):
async def _async_update_consumption_history(self) -> None:
    """Collect daily consumption measurements for prediction."""

    # Access avg_sensor data if available
    # Option A: Pull from avg_sensor entities
    for hour in range(24):
        entity_id = get_energy_average_sensor_entity_id(hour, hour + 1)
        try:
            daily_value = ha_get_entity_state_and_convert(
                self, entity_id, "float"
            )
            if daily_value is not None:
                # Sum hourly averages to get daily total
                today = datetime.now().date()
                if today not in self._measurements_history:
                    self._measurements_history[today] = 0.0
                self._measurements_history[today] += daily_value
        except Exception:
            continue

    # Option B: If you have a utility meter tracking daily consumption
    # Pull the daily total directly from the utility meter sensor


# ============================================================================
# STEP 3: COMPUTE ADAPTIVE PREDICTION
# ============================================================================

async def _async_compute_consumption_prediction(self) -> float:
    """
    Compute improved consumption prediction using adaptive filter.

    Returns:
        Predicted consumption in kWh, or fallback to old method if insufficient data
    """
    # Get prediction using adaptive model
    if self._measurements_history:
        adaptive_prediction = self._adaptive_predictor.predict(
            self._measurements_history
        )

        if adaptive_prediction is not None:
            # Cache for attribute reporting
            self._last_adaptive_prediction = adaptive_prediction
            self._last_prediction_confidence = (
                self._adaptive_predictor.prediction_confidence
            )

            await async_logger(
                self,
                "Consumption prediction: {:.1f} kWh (confidence: {:.0%})".format(
                    adaptive_prediction,
                    self._last_prediction_confidence,
                ),
                "debug",
            )

            return adaptive_prediction

    # Fallback: use old weighted average if adaptive prediction fails
    return self._compute_traditional_weighted_average()


# ============================================================================
# STEP 4: UPDATE RECOMMENDATION LOGIC
# ============================================================================

async def _async_generate_recommendations(self) -> None:
    """
    Generate hourly recommendations using improved consumption prediction.

    Key improvements:
    1. Use adaptive prediction instead of static weights
    2. Use confidence score to adjust recommendation aggressiveness
    3. Decompose prediction to understand factors affecting consumption
    """

    # Get improved consumption prediction
    predicted_consumption_kwh = await self._async_compute_consumption_prediction()
    confidence = self._last_prediction_confidence

    # If high confidence, use more aggressive optimization
    # If low confidence, use conservative strategies
    aggressiveness_factor = 0.7 + (0.3 * confidence)  # Range: 0.7 - 1.0

    # Example: Adjust discharge power based on prediction confidence
    if confidence > 0.8:
        # High confidence: can safely discharge more
        discharge_buffer = 10  # kWh (smaller, aggressive)
    elif confidence > 0.6:
        # Medium confidence: moderate buffer
        discharge_buffer = 15  # kWh
    else:
        # Low confidence: conservative approach
        discharge_buffer = 20  # kWh (larger, safe)

    # Generate recommendations for each hour
    for hour_offset in range(self._hsem_recommendation_interval_length):
        recommendation = HourlyRecommendation(
            hour_offset=hour_offset,
            prediction_consumption_kwh=predicted_consumption_kwh,
            prediction_confidence=confidence,
            discharge_buffer_kwh=discharge_buffer,
        )

        self._hourly_recommendations.append(recommendation)


# ============================================================================
# STEP 5: EXPORT PREDICTION DATA TO ATTRIBUTES
# ============================================================================

@property
def extra_state_attributes(self) -> dict:
    """Return extended state attributes including improved predictions."""

    attrs = {
        # ... existing attributes ...

        # New adaptive prediction attributes
        "consumption_prediction_adaptive_kWh": self._last_adaptive_prediction,
        "consumption_prediction_confidence": self._last_prediction_confidence,
        "consumption_measurements_count": len(self._measurements_history),

        # Show component breakdown if available (Phase 3 enhancement)
        "consumption_baseline_kWh": None,  # TODO: Phase 2/3
        "consumption_weather_factor": None,
        "consumption_solar_factor": None,
        "consumption_ev_factor": None,
    }

    return attrs


# ============================================================================
# STEP 6: IMPROVE THRESHOLD CALCULATIONS
# ============================================================================

def _calculate_improved_threshold(
    self,
    base_threshold: float,
    prediction: float,
    confidence: float,
) -> float:
    """
    Calculate dynamic threshold based on prediction confidence.

    Args:
        base_threshold: Default threshold (e.g., from config)
        prediction: Predicted consumption in kWh
        confidence: Prediction confidence [0, 1]

    Returns:
        Adjusted threshold accounting for prediction uncertainty
    """

    # Higher confidence → tighter (lower) threshold
    # Lower confidence → looser (higher) threshold

    uncertainty_factor = 1.0 - (0.3 * confidence)  # Range: 0.7 - 1.0

    adjusted_threshold = base_threshold * uncertainty_factor

    # Also consider prediction vs typical consumption
    if prediction > 0:
        # If prediction is unusually high, widen threshold
        if prediction > base_threshold * 1.5:
            adjusted_threshold *= 1.2
        # If prediction is unusually low, tighten threshold
        elif prediction < base_threshold * 0.5:
            adjusted_threshold *= 0.9

    return adjusted_threshold


# ============================================================================
# STEP 7: HANDLE EDGE CASES AND ANOMALIES
# ============================================================================

async def _async_handle_anomalous_consumption(
    self,
    current_consumption: float,
    predicted_consumption: float,
    confidence: float,
) -> None:
    """
    Detect and handle anomalous consumption patterns.

    If actual consumption deviates significantly from prediction,
    update predictor state and adjust recommendations.
    """

    if predicted_consumption <= 0:
        return

    deviation_pct = abs(current_consumption - predicted_consumption) / predicted_consumption

    # Significant anomaly detected
    if deviation_pct > 0.3:  # 30% deviation

        await async_logger(
            self,
            (
                f"Consumption anomaly detected: "
                f"actual={current_consumption:.1f} kWh, "
                f"predicted={predicted_consumption:.1f} kWh "
                f"({deviation_pct:+.0%})"
            ),
            "warning",
        )

        # Kalman filter handles this automatically (Phase 2+)
        # For Phase 1, just log and monitor

        # Could trigger:
        # 1. More conservative discharge strategy
        # 2. Increased prediction buffer
        # 3. Request for user feedback (EV arrival, unusual activity?)


# ============================================================================
# STEP 8: OPTIONAL - PHASE 2 ENHANCEMENT: KALMAN FILTER
# ============================================================================

# When ready to upgrade to Phase 2, replace AdaptiveConsumptionPredictor
# with ConsumptionKalmanFilter (from POWER_PREDICTION_IMPROVEMENTS.md)

# The interface is identical:
# from adaptive_consumption_kalman_filter import ConsumptionKalmanFilter
# self._consumption_filter = ConsumptionKalmanFilter()
#
# # Update with measurement
# self._consumption_filter.update(measured_consumption)
#
# # Get prediction with uncertainty
# prediction, confidence = self._consumption_filter.predict()


# ============================================================================
# CONFIGURATION ADDITIONS
# ============================================================================

# Add to const.py:
ADAPTIVE_CONSUMPTION_TAU_DAYS = "hsem_adaptive_consumption_tau_days"
ADAPTIVE_CONSUMPTION_MIN_SAMPLES = "hsem_adaptive_consumption_min_samples"
ADAPTIVE_CONSUMPTION_ENABLED = "hsem_adaptive_consumption_enabled"

# Add to config_flow.py (options schema):
vol.Optional(
    ADAPTIVE_CONSUMPTION_ENABLED,
    default=True,
): cv.boolean,

vol.Optional(
    ADAPTIVE_CONSUMPTION_TAU_DAYS,
    default=7.0,
): cv.positive_float,

vol.Optional(
    ADAPTIVE_CONSUMPTION_MIN_SAMPLES,
    default=3,
): cv.positive_int,


# ============================================================================
# TESTING AND VALIDATION
# ============================================================================

# 1. Test without adaptive (baseline)
# 2. Enable adaptive with Phase 1 (exponential decay)
# 3. Compare recommendation quality over 2 weeks
# 4. Measure RMSE improvement using validation_framework.py
# 5. If >10% improvement, tune tau_days parameter
# 6. Plan Phase 2 upgrade if confidence is low (<60%)


# ============================================================================
# MONITORING AND OBSERVABILITY
# ============================================================================

# New sensor attributes to expose in Home Assistant:
#
# sensor.hsem_working_mode:
#   - consumption_prediction_adaptive_kWh: 45.2 (improved prediction)
#   - consumption_prediction_confidence: 0.82 (how confident we are)
#   - consumption_measurements_count: 42 (how much history we have)
#   - consumption_prediction_old_method: 46.1 (for comparison)
#   - consumption_deviation_last_24h: +5.2% (if actual vs predicted)
#
# These help users understand:
# 1. Is the prediction reliable?
# 2. Have we collected enough data?
# 3. Is consumption changing unexpectedly?


# ============================================================================
# INTEGRATION STEPS SUMMARY
# ============================================================================

"""
Quick Implementation Checklist:

Phase 1: Add Adaptive Predictor
  [ ] Import AdaptiveConsumptionPredictor in working_mode_sensor.py
  [ ] Initialize in __init__ with tau_days=7.0
  [ ] Collect daily consumption in _measurements_history
  [ ] Replace old prediction with adaptive in _async_compute_consumption_prediction()
  [ ] Export confidence score to attributes
  [ ] Test with 14 days of historical data

Phase 1.5: Tune Parameters
  [ ] Run validation_framework.py to find optimal tau_days
  [ ] Compare RMSE: adaptive vs. traditional weighted average
  [ ] Adjust discharge_buffer based on confidence
  [ ] Monitor recommendation quality in Home Assistant

Phase 2: Upgrade to Kalman Filter (optional)
  [ ] Implement ConsumptionKalmanFilter (from POWER_PREDICTION_IMPROVEMENTS.md)
  [ ] Replace AdaptiveConsumptionPredictor with Kalman filter
  [ ] Re-validate with same test set
  [ ] Measure confidence convergence over time

Phase 3: Multi-Modal Decomposition (optional)
  [ ] Separate consumption components (weather, solar, EV)
  [ ] Learn each component independently
  [ ] Provide explainable predictions to users
  [ ] Integrate with anomaly detection
"""
