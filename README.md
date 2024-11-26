# Huawei Solar Energy Management

[![GitHub Release][releases-shield]][releases]
[![GitHub Downloads][downloads-shield]][downloads]
[![License][license-shield]][license]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

![Icon](assets/icon.png)

## Derived from the Huawei Solar Battery Optimization Project

This integration was inspired by and derived from the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations), which was developed to optimize battery usage and maximize self-consumption in solar systems. While that project focuses on managing Huawei solar batteries, this integration adapts the smoothing techniques from it to handle any type of fluctuating sensor data, whether related to energy or other measurements.

## Summary

The **Huawei Solar Energy Management (HSEM)** integration is a comprehensive solution designed to elevate the intelligence and efficiency of Huawei solar battery systems within Home Assistant. Inspired by the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations), this package not only maximizes energy self-consumption but also fine-tunes energy storage and grid interactions based on dynamic electricity prices, solar forecasting, and seasonal conditions.

HSEM employs advanced logic to align battery charging and discharging with real-time energy costs, TOU (Time-of-Use) schedules, and solar production forecasts. It actively prevents grid exports when unfavorable prices are detected and automatically shifts modes to prioritize self-consumption or TOU management. During the winter months, HSEM optimizes battery charging to fill to a SOC (State of Charge) cutoff threshold during the most cost-effective hours, while in warmer seasons, it maximizes self-consumption to make the most of solar production.

Key benefits include:

- **Cost-Efficient Energy Management**: Seamlessly aligns battery and grid interactions with real-time pricing, maximizing savings on energy costs.
- **Enhanced EV Charging Strategy**: Automatically adapts TOU schedules and discharge prevention while charging EVs, ensuring optimized use of solar and off-peak rates.
- **Seasonal Adjustments**: Automatically switches between TOU and Maximize Self Consumption (MSC) modes based on solar production and seasonal needs, enabling an intelligent response to changing daylight and weather patterns.
- **Winter-Specific Charging Optimization**: For colder months, charges the battery in the lowest-cost periods overnight and mid-day, ensuring the battery is optimally charged for high demand and peak periods.
- **Solar-Driven Energy Independence**: Promotes solar self-sufficiency, enabling homes to leverage their solar investment while contributing to a sustainable, low-carbon future.

Whether for tech-savvy homeowners or sustainability-focused users, the HSEM integration is engineered to bring robust, automated control over solar energy, delivering enhanced autonomy, financial savings, and environmental impact.

Perfect for advanced Home Assistant users, this integration taps into Huawei solar capabilities to bring state-of-the-art energy management right to your home.

---

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

### Automatic Operational Adjustments

- **Solar Self-Consumption Maximization**: Automatically shifts modes to prioritize self-consumption when solar production is high.
- **Time-of-Use Adjustments**: Changes modes based on time-of-use rates and seasonal conditions to optimize cost-efficiency.

---

## Requirements

To use this package, you need the following integrations:

- [Huawei Solar integration by wlcrs](https://github.com/wlcrs/huawei_solar)
- [Solcast integration by oziee](https://github.com/BJReplay/ha-solcast-solar)
- [Energi Data Service integration by MTrab](https://github.com/MTrab/energidataservice)

### Optional integrations

- [Huawei Solar PEES package by JensenNick](https://github.com/JensenNick/huawei_solar_pees) (optional but recommended)
- [Smoothing Analytics Sensors by woopstar](https://github.com/woopstar/smoothing_analytics_sensors) (optional but recommended)

### Default disabled sensors

The [Huawei Solar integration by wlcrs](https://github.com/wlcrs/huawei_solar) provides `sensor.inverter_active_power_control` and `sensor.batteries_rated_capacity` but it is disabled by default. To use this entity, go to the device settings, select the inverter and show hidden/disabled entities. Find the `sensor.inverter_active_power_control` and `sensor.batteries_rated_capacity` and enable it.

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

## Working Modes

This package supports various working modes to optimize your Huawei solar battery system. The table below outlines each mode, with descriptions for when and why to use them.

| Working Mode               | Description                                                                                                                                       | Use Case                                                                                                               |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| **Force Export**           | Forces the battery to charge when import prices are negative to maximize returns from exporting energy to the grid.                               | Beneficial when grid import prices are negative, enabling profit from energy exports.                                 |
| **Force Batteries Charge** | Charges the battery during specified times, often when electricity rates are low or solar production is high.                                     | Useful for preparing the battery for high demand periods or taking advantage of low-cost charging times.               |
| **EV Smart Charging**      | Avoids battery discharge when the EV charger is active, ensuring solar power or low-cost electricity fuels the EV charge.                         | Ideal for households with EVs, ensuring EVs are charged without impacting battery reserves during charging.            |
| **Force Batteries Discharge** | Discharges the battery to supply power during high-demand or high-cost periods, even overriding other settings.                             | Useful to reduce grid reliance during peak cost times or to meet specific power needs during demand spikes.            |
| **Maximize Self Consumption** | Uses solar energy to cover household consumption and charges the battery with excess production, reducing grid dependency.                  | Suitable for maximizing solar usage, particularly when net consumption is positive or during high solar production.    |
| **Time of Use**            | Adjusts battery charge/discharge periods based on time-of-use (TOU) rates, with seasonal settings prioritizing TOU in winter/spring and MSC in summer. | Ideal for users with time-of-use tariffs to optimize cost-efficiency based on seasonal adjustments.                     |

---

## Working Mode Sensor Attributes

The Working Mode Sensor provides a variety of attributes for detailed monitoring and optimization of your Huawei solar energy system. Below is a description of each attribute:

| Attribute                                    | Description                                                                                                                 |
|----------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| **read_only**                                | If HSEM is running in Read Only Mode                                                                                        |
| **last_updated**                             | The last timestamp when the sensor was updated.                                                                             |
| **last_changed_mode**                        | The last timestamp when the working mode was changed.                                                                       |
| **unique_id**                                | Unique identifier for the working mode sensor entity.                                                                       |
| **huawei_solar_device_id_inverter_1_id**     | Device ID for the primary Huawei solar inverter.                                                                            |
| **huawei_solar_device_id_inverter_2_id**     | Device ID for the secondary Huawei solar inverter, if applicable.                                                           |
| **huawei_solar_device_id_batteries_id**      | Device ID for the Huawei solar batteries.                                                                                   |
| **huawei_solar_batteries_working_mode_entity** | Entity ID for the working mode of the Huawei solar batteries.                                                             |
| **huawei_solar_batteries_working_mode_state** | Current working mode state of the Huawei solar batteries.                                                                  |
| **huawei_solar_batteries_state_of_capacity_entity** | Entity ID for the state of capacity of the Huawei solar batteries.                                                   |
| **huawei_solar_batteries_state_of_capacity_state** | Current state of capacity of the Huawei solar batteries as a percentage.                                              |
| **huawei_solar_batteries_grid_charge_cutoff_soc_entity** | Entity ID for the battery grid charge cutoff SOC (State of Charge).                                             |
| **huawei_solar_batteries_grid_charge_cutoff_soc_state** | Current cutoff SOC for charging batteries from the grid.                                                         |
| **huawei_solar_batteries_maximum_charging_power_entity** | Entity ID for the maximum charging power allowed for the Huawei solar batteries.                                |
| **huawei_solar_batteries_maximum_charging_power_state** | Current maximum charging power for the Huawei solar batteries in watts.                                          |
| **huawei_solar_inverter_active_power_control_state_entity** | Entity ID for the active power control mode of the Huawei solar inverter.                                    |
| **huawei_solar_inverter_active_power_control_state_state** | Current active power control mode state of the Huawei solar inverter.                                         |
| **huawei_solar_batteries_tou_charging_and_discharging_periods_entity** | Entity ID for the Time-of-Use (TOU) charging and discharging periods for the batteries.           |
| **huawei_solar_batteries_tou_charging_and_discharging_periods_state** | Current TOU charging and discharging periods state.                                                |
| **huawei_solar_batteries_tou_charging_and_discharging_periods_periods** | List of TOU periods configured for battery charging and discharging.                             |
| **house_consumption_power_entity**           | Entity ID for the power consumed by the household.                                                                          |
| **house_consumption_power_state**            | Current power consumption of the household in watts.                                                                        |
| **solar_production_power_entity**            | Entity ID for the power generated by solar panels.                                                                          |
| **solar_production_power_state**             | Current power generation from solar panels in watts.                                                                        |
| **net_consumption**                          | Net power consumption, taking into account both solar production and household consumption.                                 |
| **net_consumption_with_ev**                  | Net power consumption including the power consumed by the EV charger, if active.                                            |
| **energi_data_service_import_entity**        | Entity ID for the energy data service import price.                                                                         |
| **energi_data_service_import_state**         | Current state of the energy import price.                                                                                   |
| **energi_data_service_export_entity**        | Entity ID for the energy data service export price.                                                                         |
| **energi_data_service_export_value**         | Current state of the energy export price.                                                                                   |
| **battery_max_capacity**                     | Maximum energy capacity of the battery in kilowatt-hours (kWh).                                                             |
| **battery_remaining_charge**                 | Current remaining charge in the battery in kilowatt-hours (kWh).                                                            |
| **battery_conversion_loss**                  | Conversion loss percentage for charging/discharging the battery, representing efficiency losses.                            |
| **ev_charger_status_entity**                 | Entity ID for the EV charger’s status.                                                                                      |
| **ev_charger_status_state**                  | Current status of the EV charger (e.g., active or inactive).                                                                |
| **ev_charger_power_entity**                  | Entity ID for the power consumed by the EV charger.                                                                         |
| **ev_charger_power_state**                   | Current power consumption by the EV charger in watts.                                                                       |
| **house_power_includes_ev_charger_power**    | Boolean indicating if the house power consumption includes power used by the EV charger.                                    |
| **solcast_pv_forecast_forecast_today_entity** | Entity ID for the Solcast PV forecast data for today.                                                                      |
| **energy_needs**                             | The amount of calculated energy needs in different periods during the day                                                   |
| **hourly_calculations**                      | Dictionary containing hourly data for house consumption, solar forecast, net consumption, and pricing information.          |

[releases-shield]: https://img.shields.io/github/v/release/woopstar/hsem?style=for-the-badge
[releases]: https://github.com/woopstar/hsem/releases
[downloads-shield]: https://img.shields.io/github/downloads/woopstar/hsem/total.svg?style=for-the-badge
[downloads]: https://github.com/woopstar/hsem/releases
[license-shield]: https://img.shields.io/github/license/woopstar/hsem?style=for-the-badge
[license]: https://github.com/woopstar/hsem/blob/main/LICENSE
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-FFDD00.svg?style=for-the-badge&logo=buymeacoffee
[buymecoffee]: https://www.buymeacoffee.com/woopstar
