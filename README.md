# Huawei Solar Energy Management

[![GitHub Release][releases-shield]][releases]
[![GitHub Downloads][downloads-shield]][downloads]
[![License][license-shield]][license]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

![Icon](assets/icon.png)

## Features

### Dynamic Grid Export/Import Management

- **Negative Export Prices**: Avoids grid export when export prices are negative.
- **Negative Import Prices**: Forces battery charging when import prices are negative, prioritizing cost savings.

### EV Charging Optimization

- **Smart TOU Mode**: Activates a dedicated TOU mode during EV charging to manage costs effectively.
- **Battery Discharge Prevention**: Disables battery discharge while the EV is charging, preserving stored energy for other uses.

### Seasonal Mode Switching

- **Maximize Self Consumption (MSC)**: Switches to MSC when solar production exceeds home consumption, maximizing solar utilization.
- **Automatic Seasonal Mode Selection**: Adjusts modes based on season—TOU for winter/spring, MSC for summer.

### Advanced Winter Charging Strategy

- **Low-Cost Battery Charging**: In winter months, schedules battery charging to the grid SOC cutoff percentage during the most affordable time frames between **00:00 - 06:00** and **12:00 - 17:00**, ensuring battery readiness for high-demand periods.

### Optimized Day and Night Charging Strategy

- **Smart Charging Time Selection**: Automatically finds the cheapest time to charge the battery based on:
  - **Import Prices**: Prefers negative or low import prices.
  - **Solar Surplus Forecast**: Calculates expected solar surplus and schedules charging accordingly.
- **Day and Night Periods**: Allows configuration of both day and night charging periods to fully optimize energy costs.

### Automatic Operational Adjustments

- **Solar Self-Consumption Maximization**: Automatically shifts modes to prioritize self-consumption when solar production is high.
- **Time-of-Use Adjustments**: Changes modes based on time-of-use rates and seasonal conditions to optimize cost-efficiency.

### Input Validation and Error Handling

- **Missing Input Entities Detection**: Automatically detects missing or misconfigured input sensors, providing clear error descriptions to assist with troubleshooting.

---

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

## Working Mode Sensor States

This package supports various working modes to optimize your Huawei solar battery system. The table below outlines each mode, with descriptions for when and why to use them.

| Working Mode               | Description                                                                                                                                       | Use Case                                                                                                               |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| **Force Export**           | Forces the battery to charge when import prices are negative to maximize returns from exporting energy to the grid.                               | Beneficial when grid import prices are negative, enabling profit from energy exports.                                 |
| **Force Batteries Charge** | Charges the battery during specified times, often when electricity rates are low or solar production is high.                                     | Useful for preparing the battery for high demand periods or taking advantage of low-cost charging times.               |
| **EV Smart Charging**      | Avoids battery discharge when the EV charger is active, ensuring solar power or low-cost electricity fuels the EV charge.                         | Ideal for households with EVs, ensuring EVs are charged without impacting battery reserves during charging.            |
| **Force Batteries Discharge** | Discharges the battery to supply power during high-demand or high-cost periods, even overriding other settings.                             | Useful to reduce grid reliance during peak cost times or to meet specific power needs during demand spikes.            |
| **Maximize Self Consumption** | Uses solar energy to cover household consumption and charges the battery with excess production, reducing grid dependency.                  | Suitable for maximizing solar usage, particularly when net consumption is positive or during high solar production.    |
| **Time of Use**            | Adjusts battery charge/discharge periods based on time-of-use (TOU) rates, with seasonal settings prioritizing TOU in winter/spring and MSC in summer. | Ideal for users with time-of-use tariffs to optimize cost-efficiency based on seasonal adjustments.                     |
| **Missing Entities Input** | Detects missing entitie from configuration or entities not reporting state required for proper system operation.            | Useful for troubleshooting integrations or identifying misconfigurations in sensor setups.                             |

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
