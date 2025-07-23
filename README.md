# Huawei Solar Energy Management (HSEM) for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![GitHub Downloads][downloads-shield]][downloads]
[![License][license-shield]][license]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

![Icon](assets/icon.png)

## Introduction

**Huawei Solar Energy Management (HSEM)** is a modular, secure, and highly configurable Home Assistant integration for optimizing Huawei solar batteries, inverters, and related energy devices. HSEM automates battery charging/discharging, grid export/import, and adapts to dynamic energy prices, solar forecasts, and EV charging events.

**Terminology:**
- **Battery:** Refers to your solar battery (e.g., Huawei battery), not EV battery.
- **EV Charger:** Refers to any wall charger integration for electric vehicles.
- **Grid Import/Export:** Import means buying electricity from the grid; export means selling electricity to the grid.

---

## Quick Start

1. **Remove any previous Huawei Solar Battery Optimization Project integrations.**
2. **Install HSEM** via Home Assistant's custom integrations or manually.
3. **Configure your sensors** for solar battery, inverter, grid, and EV charger (if present).
4. **Set up battery schedules** in HSEM (do not use Fusion Solar app for scheduling).
5. **Let HSEM run for at least 14 days** to collect historical data for optimal performance.
6. **Monitor the Working Mode Sensor** for system status and recommendations.

**Tip:**
If you are a new user and want to safely observe how HSEM would control your battery system without making any changes, enable the **Read-Only** mode. This acts as a "dry run" and allows you to review all proposed configuration changes before they are applied.

For detailed setup instructions, see the [Documentation](#documentation).

## Requirements

To use this package, you need the following integrations:

- [Huawei Solar integration by wlcrs](https://github.com/wlcrs/huawei_solar) **VERSION 1.5.0a1 REQUIRED**
- [Solcast integration by oziee](https://github.com/BJReplay/ha-solcast-solar)
- [Energi Data Service integration by MTrab](https://github.com/MTrab/energidataservice)

### Optional integrations

- [Huawei Solar PEES package by JensenNick](https://github.com/JensenNick/huawei_solar_pees) (optional but recommended)
- [Smoothing Analytics Sensors by woopstar](https://github.com/woopstar/smoothing_analytics_sensors) (optional but recommended)

### Default disabled sensors

The [Huawei Solar integration by wlcrs](https://github.com/wlcrs/huawei_solar) provides `sensor.inverter_active_power_control` and `sensor.batteries_rated_capacity` but they are disabled by default. To use these entities, go to the device settings, select the inverter or batteries device and show hidden/disabled entities. Find the `sensor.inverter_active_power_control` and `sensor.batteries_rated_capacity` and enable them.

---

## Installation

### Method 1: HACS (Home Assistant Community Store)

1. In HACS, go to **Integrations**.
2. Click the three dots in the top-right corner, and select **Custom repositories**.
3. Add this repository URL and select **Integration** as the category:
   `https://github.com/woopstar/hsem`
4. Click **Add**.
5. The integration will now appear in HACS under the **Integrations** section. Click **Install**.
6. Restart Home Assistant.

### Method 2: Manual Installation

1. Copy the `hsem` folder to your `custom_components` folder in your Home Assistant configuration.
2. Restart Home Assistant.
3. Add the integration via the Home Assistant integrations page and configure your settings.

---

## Documentation

- [FAQ](#frequently-asked-questions-faq)
  *Answers to common questions about HSEM usage and setup.*
- [Feature Overview](#features)
  *Summary of all major features provided by HSEM.*
- [How to Calculate the Minimum Charging Price for a Battery Schedule](https://github.com/woopstar/hsem/wiki/How-to-Calculate-the-Minimum-Charging-Price-for-a-Battery-Schedule)
  *Learn how to factor battery depreciation cost into your schedule optimization.*
- [Dashboard 3.0 Setup](https://github.com/woopstar/hsem/wiki/Dashboard-3.0)
  *Step-by-step guide for setting up the HSEM dashboard in Home Assistant.*
- [Best Practices](#best-practices)
  *Recommended configuration and operational tips for HSEM.*
- [Battery Schedules](#battery-schedules)
  *How to configure and use battery discharge schedules.*
- [Weighted & Average Consumption Sensors](#weighted--average-consumption-sensors)
  *How HSEM calculates expected house consumption using custom sensors.*
- [Working Mode Sensor States](#working-mode-sensor-states)
  *Descriptions of all supported working modes and their use cases.*
- [Working Mode Sensor Logic](#working-mode-sensor-logic)
  *Detailed explanation of how the working mode sensor makes decisions.*
- [Special Configuration Options](#special-configuration-options)
  *Advanced options for battery and EV charger control.*

---

## Frequently Asked Questions (FAQ)

### Is HSEM a replacement for the Huawei Solar Battery Optimization Project?

**Yes.** HSEM is a full replacement for the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations). You should **remove** the old optimization project before installing HSEM. HSEM includes all necessary features and improvements.

### What does "battery" refer to in this integration?

When "battery" is mentioned in this documentation or the integration, it **always refers to your solar battery** (e.g., Huawei solar battery), **not your EV battery**.

### Does HSEM optimize EV charging?

HSEM **does not provide direct EV charging optimization**. Instead, it monitors your EV charger's power sensor (from any wall charger integration) to ensure that your EV is not charged from the house battery. HSEM disables battery discharge while the EV charger is active, but it does **not** estimate how much charge your EV needs or prioritize charging hours for the EV.

### How does HSEM use EV charger data in its calculations?

HSEM uses the EV charger status and power sensors to determine when your EV is charging and how much power is being consumed.
- If your house consumption sensor **includes** EV charger power, HSEM subtracts the EV charger power from net consumption to avoid double-counting.
- If your house consumption sensor **excludes** EV charger power, HSEM adds the EV charger power to net consumption.
- This ensures that battery discharge and optimization decisions are based on accurate household load, preventing the battery from discharging into the EV unless explicitly allowed.

### Can I force the battery to discharge into my EV?

Yes.
Enable `hsem_ev_charger_force_max_discharge_power` in the configuration to allow the battery to discharge at a specified maximum power (`hsem_ev_charger_max_discharge_power`) when the EV charger is active.
This is useful if you want to maximize self-consumption and charge your EV from solar/battery rather than the grid.

### What happens if I set the EV charger status or power sensor incorrectly?

If the EV charger sensors are misconfigured or missing, HSEM may not correctly detect EV charging events.
- This can lead to unwanted battery discharge into the EV or inaccurate net consumption calculations.
- Always verify that your EV charger status and power sensors are correctly set up and reporting valid states.

### Should I configure time-of-use (TOU) settings in the Fusion Solar app?

**No.** You do **not** need to configure anything in the Fusion Solar app. HSEM calculates and manages all battery schedules and time slots automatically. Any time slots set in the Fusion Solar app will be **overwritten** by HSEM. The only schedules that matter are those you set up in HSEM.

### How does HSEM interact with Fusion Solar time slots?

HSEM will **calculate and rewrite** the time slots in Fusion Solar as needed. You do not need to manually adjust or delete time slots in the Fusion Solar app. HSEM takes full control of scheduling based on your configuration and sensor data.

### How long does it take for HSEM to optimize my system?

Allow HSEM to run for **at least 14 days** to collect enough historical energy sensor data for accurate optimization and forecasting.

### Is there a "dry run" mode for safe testing?

**Yes.**
HSEM includes a **Read-Only** mode (sometimes called "dry run mode"). When enabled, HSEM will **not** send any commands to your devices or change battery configurations. Instead, it will show you the proposed actions and recommendations based on your sensor data and schedules.
This is especially useful for new users or during the first 14 days, while HSEM is collecting historical data for optimization.

To enable Read-Only mode, use the provided switch entity in Home Assistant.
When ready, disable Read-Only mode to allow HSEM to actively manage your battery system.

### What if my house consumption sensor includes or excludes EV charger power?

You can configure HSEM to match your sensor setup:
- Set `hsem_house_power_includes_ev_charger_power` to **true** if your house consumption sensor already includes EV charger power.
- Set it to **false** if your house consumption sensor excludes EV charger power.
- HSEM will adjust net consumption calculations accordingly to ensure accurate optimization.

### Can I manually override the working mode?

Yes.
Use the `hsem_force_working_mode` select entity in Home Assistant to manually set the working mode (e.g., Force Export, Force Charge, EV Smart Charging, etc.).
Set it to "auto" to return to normal automatic optimization.

### What happens if required sensors are missing or unavailable?

HSEM will enter a "Missing Entities Input" state and provide a clear error description in the sensor attributes.
No changes will be made to your battery or inverter until all required sensors are available and reporting valid states.

---

## Features

- **Dynamic Grid Export/Import Management**
  - Avoids grid export when export prices are negative.
  - Forces battery charging when import prices are negative.
  - Configurable minimum export price threshold.
  - Automatic grid export power control for multiple inverters.

- **EV Charging Optimization**
  - Smart TOU mode during EV charging.
  - Disables battery discharge while EV is charging.
  - Option to force max battery discharge power during EV charging.
  - Supports EV charger status and power sensors.

- **Battery Scheduling**
  - Up to three configurable battery discharge schedules.
  - Minimum price difference for schedule activation.
  - Calculates required battery capacity and cost for each schedule.
  - Finds best charging times before scheduled discharges.
  - **Considers battery depreciation cost per kWh** for economic optimization.
    See [this guide](https://github.com/woopstar/hsem/wiki/How-to-Calculate-the-Minimum-Charging-Price-for-a-Battery-Schedule) for details.

- **Weighted Consumption Forecasting**
  - Hourly house consumption averages over 1, 3, 7, and 14 days.
  - Weighted values smooth out anomalies and improve prediction accuracy.
  - Modular sensor support for custom averaging.

- **Solar Forecast Integration**
  - Solcast PV forecast sensors for today and tomorrow.
  - Hourly solar production estimates used in optimization.

- **Seasonal Mode Switching**
  - Maximize Self Consumption (MSC) in summer.
  - Time-of-Use (TOU) mode in winter/spring.
  - Automatic seasonal adjustment.

- **Advanced Charging Strategies**
  - Winter: Charges battery to grid SOC cutoff during cheapest periods (00:00-06:00, 12:00-17:00).
  - Smart selection of charging times based on import prices and solar surplus.
  - Day and night charging periods configurable.

- **Automatic Operational Adjustments**
  - Solar self-consumption maximization.
  - Time-of-use adjustments for cost efficiency.
  - Automatic recommendations for battery charge/discharge/export.

- **Input Validation and Error Handling**
  - Detects missing or misconfigured input sensors.
  - Clear error descriptions for troubleshooting.
  - Read-only mode for safe testing.

- **Secure and Modular Design**
  - No hardcoded credentials.
  - Modular code for easy extension and maintenance.
  - Optimized for performance and reliability.

---

## Working Mode Sensor States

The HSEM Working Mode Sensor supports multiple states to optimize your solar battery system. Below is a summary of all supported modes and their descriptions:

| Working Mode                   | Description                                                                                                         | Use Case                                                                                                               |
|------------------------------- |---------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| **Time Passed**                | The time has passed and no recommendations are given for the hour.              | When calculating the recommendations, this is the default recommendation for hours that have already passed during the day.  |
| **Force Export**               | Forces battery charging when grid import prices are negative (i.e., you are paid to consume electricity), then exports stored energy to the grid for profit. | Beneficial when grid import prices are negative, allowing you to charge your battery at a profit and export energy.    |
| **Force Batteries Charge**     | Charges battery during specified times, often when rates are low or solar is high.                                  | Preparing battery for high demand or taking advantage of low-cost charging times.                                      |
| **EV Smart Charging**          | Avoids battery discharge when EV charger is active, ensuring solar or low-cost electricity fuels the EV.            | For households with EVs, ensuring EVs are charged without impacting battery reserves.                                  |
| **Force Batteries Discharge**  | Discharges battery to supply power during high-cost periods, overriding other settings.                             | Reducing grid reliance during peak cost times or meeting specific power needs.                                         |
| **Batteries Charge Solar**     | Charges battery from solar surplus when available.                                                                  | When solar production exceeds consumption, storing excess energy.                                                      |
| **Batteries Charge Grid**      | Charges battery from grid during periods of low or negative import prices.                                          | When grid prices are low or negative, storing energy for later use.                                                    |
| **Batteries Discharge Mode**   | Discharges battery to cover load during scheduled periods.                                                          | Scheduled battery discharge to minimize grid import during expensive periods.                                          |
| **Batteries Wait Mode**        | Battery remains idle, waiting for optimal charge/discharge conditions.                                              | Used in winter/spring or when no action is optimal.                                                                   |
| **Missing Entities Input**     | Indicates missing or misconfigured input sensors required for operation.                                            | Troubleshooting integrations or identifying misconfigurations in sensor setups.                                        |
| **Read Only**                  | Integration is in read-only (dry run) mode, no commands sent to devices.                                            | Safe testing or monitoring without affecting device states.                                                            |

---

## Working Mode Sensor Logic

The Working Mode Sensor is the core decision engine of HSEM. It continuously analyzes sensor data, forecasts, and user schedules to determine the optimal operating mode for your solar battery system.

**How it works:**

Every update cycle, the sensor performs the following steps:

1. **Configuration & State Fetching**
   - Reloads configuration options and fetches the latest states from all required sensors (battery, inverter, grid prices, solar production, EV charger, etc.).
   - Checks for missing or unavailable input entities and sets an error state if needed.

2. **Pre-Calculations**
   - **Net Consumption Calculation:** Determines the net energy demand by subtracting solar production (and optionally EV charging) from house consumption.
   - **Battery Capacity Calculation:** Calculates usable and current battery capacity, considering minimum buffer and conversion losses.
   - **Weighted Consumption Forecasts:** For each hour, combines 1d, 3d, 7d, and 14d average sensors using configured weights to estimate expected house consumption.
   - **Solar Forecast Integration:** Integrates Solcast PV forecasts for hourly solar production estimates.
   - **Hourly Net Consumption:** For each hour, calculates expected net consumption (house minus solar).
   - **Import/Export Price Mapping:** Maps hourly grid import and export prices to each hour.
   - **Battery Schedules:** Calculates required battery capacity and cost for each active discharge schedule, and finds best charging times before scheduled discharges.
   - **Energy Needs:** Aggregates energy needs for key time blocks (morning, day, evening, night).

3. **Optimization Strategy**
   - For each hour, the sensor determines the optimal recommendation based on all pre-calculated data:
     - **Force Export:** If grid import price is negative, recommend charging battery from grid and exporting for profit.
     - **Charge from Grid:** If scheduled or recommended, charge battery during low/negative price periods.
     - **EV Smart Charging:** If EV charger is active, disable battery discharge or limit discharge power.
     - **Discharge Mode:** If battery schedule is active and capacity is sufficient, discharge battery to cover load.
     - **Force Discharge:** If forced, discharge battery during expensive grid periods.
     - **Charge from Solar:** If solar surplus is available, charge battery from excess solar.
     - **Wait Mode:** If no action is optimal (e.g., battery full, no economic incentive), set battery to idle.
     - **Fully Fed to Grid:** If export price is higher than import price, export all available energy to grid.
   - Recommendations are set in priority order, ensuring the most cost-effective and energy-efficient actions are chosen.

4. **Working Mode & TOU Periods Application**
   - Based on the selected recommendation, the sensor sets the battery working mode (e.g., Maximize Self Consumption, Time-of-Use).
   - If Time-of-Use mode is selected, updates the battery's TOU periods to match the recommended schedule.
   - If not in read-only mode, applies changes to the actual device entities via Home Assistant services.

5. **State & Attribute Updates**
   - Updates the sensor's state to reflect the current recommendation.
   - Exposes detailed attributes including hourly calculations, recommendations, battery schedule status, and more.
   - Triggers Home Assistant state updates for automations and dashboards.

**Summary of Decision Flow:**
1. Gather all relevant sensor and schedule data.
2. Perform all pre-calculations for consumption, solar, prices, and schedules.
3. Apply the optimization strategy to select the best action for each hour.
4. Set the working mode and recommendations, and apply changes if not in read-only mode.
5. Update sensor state and attributes for visibility and automation.

**Advanced Notes:**
- The logic is modular and extensible, allowing new recommendations or optimization strategies to be added.
- Read-only mode allows safe testing and review of proposed actions before applying changes.
- All calculations are performed hourly, but can be triggered manually or on sensor state changes.

---

## Best Practices

- **Do not configure schedules in the Fusion Solar app.** HSEM will overwrite them.
- **Ensure all required sensors are available and correctly configured.**
- **Review the FAQ and Troubleshooting sections if you encounter issues.**
- **Allow HSEM to collect data for 14 days before expecting optimal results.**

---

## Battery Schedules

Battery schedules are a core feature of HSEM, allowing you to automate when your solar battery charges and discharges based on energy prices, solar forecasts, and household consumption patterns.

**Purpose:**
- Maximize self-consumption and minimize energy costs.
- Automate battery charging during low-price periods and discharging during high-price periods.
- Prepare battery for specific events (e.g., EV charging, peak grid prices).

**Configuration:**
- You can define up to three separate discharge schedules.
- Each schedule specifies a time window for battery discharge and a minimum price difference required for activation.
- HSEM automatically calculates the required battery capacity and finds the best charging times before each scheduled discharge.
- Schedules are managed entirely within HSEM; do not configure them in the Fusion Solar app.

**Best Practices:**
- Set schedules to match your utility's peak pricing periods or your household's highest consumption times.
- Use the [Minimum Charging Price Guide](https://github.com/woopstar/hsem/wiki/How-to-Calculate-the-Minimum-Charging-Price-for-a-Battery-Schedule) to factor in battery depreciation cost per kWh.
- Review and adjust schedules periodically based on your energy usage and price trends.
- Allow HSEM to collect at least 14 days of data for optimal schedule recommendations.

**Example Schedule:**
- Discharge battery from 17:00 to 21:00 if the price difference between charging and discharging exceeds your configured threshold.

For more details and advanced configuration, see the [Configuration Guide](docs/configuration.md).

---

## Weighted & Average Consumption Sensors

HSEM uses a set of custom sensors to calculate expected house consumption for each hour of the day. These sensors track your energy usage over different time windows (1, 3, 7, and 14 days) and combine them using configurable weights to produce a more accurate and robust forecast.

**How it works:**
- For each hour (e.g., 17:00-18:00), HSEM creates four sensors:
  - 1-day average consumption
  - 3-day average consumption
  - 7-day average consumption
  - 14-day average consumption
- Each sensor calculates the average energy used in that hour over its respective period.
- HSEM then applies user-configurable weights to each average and sums them to produce a weighted expected consumption value for that hour.

**Why use weighted averages?**
- Short-term averages (1d, 3d) quickly adapt to recent changes in usage.
- Long-term averages (7d, 14d) smooth out anomalies and provide stability.
- Weighting allows you to balance responsiveness and stability for more reliable predictions.

**How to configure:**
- You can adjust the weights for each period in the HSEM configuration.
- The sum of all weights should be 100.
- Example: 1d=40, 3d=30, 7d=20, 14d=10.

**Usage in optimization:**
- The weighted expected consumption for each hour is used to:
  - Forecast household energy needs.
  - Plan battery charging/discharging schedules.
  - Optimize grid import/export and solar utilization.

For more details on sensor setup and customization, see the [Configuration Guide](docs/configuration.md).

---

## Special Configuration Options

HSEM provides several advanced options for fine-tuning battery and EV charger behavior:

### `hsem_ev_charger_force_max_discharge_power`
- **Purpose:**
  When enabled, this option forces the battery to discharge at the maximum configured power whenever the EV charger is active.
- **Use Case:**
  Ensures that your EV is charged using as much battery power as possible (rather than grid power), maximizing self-consumption and minimizing grid import during EV charging events.
- **Configuration:**
  Set this option in the HSEM configuration. You can also specify the maximum discharge power using `hsem_ev_charger_max_discharge_power`.

### `hsem_ev_charger_max_discharge_power`
- **Purpose:**
  Defines the maximum discharge power (in watts) that the battery should use when `hsem_ev_charger_force_max_discharge_power` is enabled and the EV charger is active.
- **Use Case:**
  Limits battery discharge to a safe or preferred value during EV charging, preventing over-discharge or exceeding inverter/battery limits.
- **Configuration:**
  Set this value in the HSEM configuration to match your battery/inverter capabilities or personal preferences.

### `hsem_force_working_mode`
- **Purpose:**
  Allows you to manually override the automatic optimization logic and force the battery system into a specific working mode (e.g., Force Export, Force Charge, EV Smart Charging, etc.).
- **Use Case:**
  Useful for testing, troubleshooting, or handling special scenarios where you want to temporarily bypass HSEM's recommendations.
- **Configuration:**
  Use the `hsem_force_working_mode` select entity in Home Assistant to choose a mode. Set to "auto" to return to normal automatic optimization.

---

## Derived from the Huawei Solar Battery Optimization Project

This integration was inspired by and derived from the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations), which was developed to optimize battery usage and maximize self-consumption in solar systems. While that project focuses on managing Huawei solar batteries, this integration adapts the smoothing techniques from it to handle any type of fluctuating sensor data, whether related to energy or other measurements.

[releases-shield]: https://img.shields.io/github/v/release/woopstar/hsem?style=for-the-badge
[releases]: https://github.com/woopstar/hsem/releases
[downloads-shield]: https://img.shields.io/github/downloads/woopstar/hsem/total.svg?style=for-the-badge
[downloads]: https://github.com/woopstar/hsem/releases
[license-shield]: https://img.shields.io/github/license/woopstar/hsem?style=for-the-badge
[license]: https://github.com/woopstar/hsem/blob/main/LICENSE
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-FFDD00.svg?style=for-the-badge&logo=buymeacoffee
[buymecoffee]: https://www.buymeacoffee.com/woopstar
