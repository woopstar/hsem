# Huawei Solar Energy Management (HSEM)

> **Home Assistant integration for optimizing Huawei solar batteries, inverters, and EV charging.**

HSEM is a modular, secure, and highly configurable Home Assistant integration that automates battery charging/discharging, grid export/import, and adapts to dynamic energy prices, solar forecasts, and EV charging events.

---

## Terminology

- **Battery:** Refers to your solar battery (e.g., Huawei battery), **not** your EV battery.
- **EV Charger:** Any wall charger integration for electric vehicles.
- **Grid Import/Export:** Import = buying electricity from the grid; Export = selling electricity to the grid.

---

## Quick Start

1. **Remove any previous Huawei Solar Battery Optimization Project integrations.**
2. **Install HSEM** via HACS or manually.
3. **Configure your sensors** for solar battery, inverter, grid, and EV charger (if present).
4. **Set up battery schedules** in HSEM (do not use Fusion Solar app for scheduling).
5. **Let HSEM run for at least 14 days** to collect historical data for optimal performance.
6. **Monitor the Working Mode Sensor** for system status and recommendations.

> **Tip:** New user? Enable **Read-Only** mode to safely observe what HSEM would do before letting it control your system.

---

## Features

- **Dynamic Grid Export/Import Management** — avoids export at negative prices, forces charging at negative import prices.
- **EV Charging Optimization** — smart TOU mode during EV charging, prevents battery drain into EV unless configured.
- **Battery Scheduling** — up to three configurable discharge schedules with price-difference thresholds and depreciation-aware economics.
- **Excess Battery Export** — automatically exports excess battery capacity when profitable, differentiating solar-charged vs grid-charged energy.
- **Consumption Forecasting** — legacy weighted-average (1d/3d/7d/14d with IQR outlier detection) or ML ridge regression.
- **Solar Forecast Integration** — Solcast PV forecasts for today and tomorrow.
- **Seasonal Mode Switching** — Maximize Self Consumption (summer) vs Time-of-Use (winter/spring).
- **Read-Only Mode** — full "dry run" for safe testing without sending commands to devices.

---

## Frequently Asked Questions

### Is HSEM a replacement for the Huawei Solar Battery Optimization Project?

**Yes.** HSEM is a full replacement. Remove the old optimization project before installing HSEM.

### What does "battery" refer to in this integration?

Always your **solar battery** (e.g., Huawei battery), not your EV battery.

### Does HSEM optimize EV charging?

**Yes.** When "EV Planned Load" is enabled, HSEM's EV planner computes an optimal charging schedule that charges from excess PV first, then from cheapest grid hours, completing before the configured deadline.

### How does HSEM use EV charger data in its calculations?

HSEM uses the EV charger status and power sensors to determine when your EV is charging. Depending on your sensor setup, it either subtracts or adds EV charger power to net consumption to avoid double-counting or underestimation.

### Can I force the battery to discharge into my EV?

Yes. Enable `hsem_ev_charger_force_max_discharge_power` and set `hsem_ev_charger_max_discharge_power` to allow the battery to discharge at the specified power when the EV charger is active.

### Should I configure time-of-use (TOU) settings in the Fusion Solar app?

**No.** HSEM calculates and manages all battery schedules automatically. Any time slots in the Fusion Solar app will be overwritten.

### How does HSEM interact with Fusion Solar time slots?

HSEM calculates and rewrites the time slots in Fusion Solar as needed. You do not need to manually adjust them.

### How long does it take for HSEM to optimize my system?

Allow **at least 14 days** for HSEM to collect enough historical data for accurate optimization.

### Is there a "dry run" mode for safe testing?

**Yes.** Enable Read-Only mode via the switch entity. HSEM will show proposed actions without sending commands.

### What if my house consumption sensor includes or excludes EV charger power?

Configure `hsem_house_power_includes_ev_charger_power` to match your setup. HSEM adjusts net consumption accordingly.

### Can I manually override the working mode?

Yes. Use the `hsem_force_working_mode` select entity. Set to "auto" to return to automatic optimization.

### What happens if required sensors are missing or unavailable?

HSEM enters a "Missing Entities Input" state with a clear error description. No changes are made until all sensors are available.

---

## Working Mode Sensor States

| Working Mode | Description |
|---|---|
| **Time Passed** | No recommendations — the hour has already passed. |
| **Force Export** | Charges from grid at negative prices, then exports for profit. |
| **Force Batteries Charge** | Forces discharge to export excess capacity when profitable. |
| **EV Smart Charging** | Disables battery discharge when EV charger is active. |
| **Force Batteries Discharge** | Discharges battery during high-cost periods. |
| **Batteries Charge Solar** | Charges battery from solar surplus. |
| **Batteries Charge Grid** | Charges from grid during low/negative import prices. |
| **Batteries Discharge Mode** | Scheduled battery discharge to minimize grid import. |
| **Batteries Wait Mode** | Battery idle, waiting for optimal conditions. |
| **Missing Entities Input** | Missing or misconfigured input sensors. |
| **Read Only** | Dry run mode — no commands sent to devices. |

---

## Working Mode Sensor Logic

Every update cycle, the sensor:

1. **Fetches configuration & sensor states** — battery, inverter, grid prices, solar production, EV charger.
2. **Performs pre-calculations** — net consumption, battery capacity, weighted consumption forecasts, solar forecast, hourly net consumption, battery schedules.
3. **Applies optimization strategy** — determines the best action per hour (Force Export, Charge Solar/Grid, EV Smart Charging, Discharge, Wait).
4. **Applies working mode & TOU periods** — sets battery mode and updates TOU periods if not in read-only mode.
5. **Updates state & attributes** — exposes hourly calculations, recommendations, and schedule status.

---

## Best Practices

- **Do not configure schedules in the Fusion Solar app.** HSEM overwrites them.
- **Ensure all required sensors are available and correctly configured.**
- **Allow HSEM to collect 14 days of data** before expecting optimal results.

---

## Battery Schedules

Define up to three discharge schedules with time windows and minimum price differences. HSEM automatically calculates required battery capacity and finds optimal charging times before each discharge.

**Example:** Discharge 17:00–21:00 if the price difference exceeds your configured threshold.

> See [How to Calculate the Minimum Charging Price](https://github.com/woopstar/hsem/wiki/How-to-Calculate-the-Minimum-Charging-Price-for-a-Battery-Schedule) for battery depreciation economics.

---

## Excess Battery Export

Automatically exports excess battery capacity to the grid when profitable:

1. **Calculate required battery** — minimum capacity needed for rest-of-day consumption + safety buffer.
2. **Identify excess** — any capacity above requirement is available for export.
3. **Optimize timing** — export at peak price hours, respecting max discharge rate.
4. **Differentiate energy source** — solar-charged energy exports at any positive price; grid-charged only exports if profit ≥ threshold.
5. **Economic threshold** — calculated from battery depreciation: `Depreciation = (Purchase_Price × Capacity_Loss) / (2 × Expected_Cycles × Usable_Capacity)`

### Example

- Battery: 100 kWh usable, current 95 kWh
- Consumption forecast (rest of day): 60 kWh, buffer 10%
- Required: 70 kWh → Excess: 25 kWh → Export at peak prices

---

## Weighted & Average Consumption Sensors

HSEM supports two prediction modes:

- **Legacy (default):** Weighted averages over 1d/3d/7d/14d windows with IQR outlier detection, baseline capping, and reliability scaling.
- **ML (optional):** Ridge regression querying the HA recorder directly.

### Default Weights (sum must equal 100)

| Mode | 1 Day | 3 Days | 7 Days | 14 Days |
|---|---|---|---|---|
| Balanced (default) | 25 | 30 | 30 | 15 |
| Conservative | 20 | 30 | 35 | 15 |
| Fast-reacting | 30 | 30 | 25 | 15 |

---

## Special Configuration Options

| Option | Purpose |
|---|---|
| `hsem_ev_charger_force_max_discharge_power` | Forces max battery discharge when EV is charging |
| `hsem_ev_charger_max_discharge_power` | Maximum discharge power (W) for EV charging |
| `hsem_force_working_mode` | Manually override to a specific working mode |

---

## Derived from the Huawei Solar Battery Optimization Project

This integration was inspired by and derived from the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations).
