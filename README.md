# Huawei Solar Energy Management

[![Current Release](https://img.shields.io/github/release/woopstar/hsem/all.svg?style=plastic)](https://github.com/woopstar/hsem/releases) [![Github All Releases](https://img.shields.io/github/downloads/woopstar/hsem/total.svg?style=plastic)](https://github.com/woopstar/hsem/releases)

![Huawei Solar Energy Management](assets/icon.png)

## Derived from the Huawei Solar Battery Optimization Project

This integration was inspired by and derived from the [Huawei Solar Battery Optimization Project](https://github.com/heinoskov/huawei-solar-battery-optimizations), which was developed to optimize battery usage and maximize self-consumption in solar systems. While that project focuses on managing Huawei solar batteries, this integration adapts the smoothing techniques from it to handle any type of fluctuating sensor data, whether related to energy or other measurements.

## Summary
Maximize the potential of your Huawei solar battery system with this powerful Home Assistant package. By intelligently optimizing battery usage, solar production, and grid interaction, this solution helps you:

Significantly reduce your energy costs
Maximize returns on surplus solar energy
Minimize your carbon footprint
Increase your energy independence
Perfect for homeowners looking to make the most of their solar investment while contributing to a greener future.

## Features

- Grid export management based on spot prices:
  - Enables or disables grid export based on current electricity spot prices
  - Avoids exporting during negative price periods

## Installation

### Method 1: HACS (Home Assistant Community Store)

1. In HACS, go to **Integrations**.
2. Click the three dots in the top-right corner, and select **Custom repositories**.
3. Add this repository URL and select **Integration** as the category:
   `https://github.com/woopstar/hsem`
4. Click **Add**.
5. The integration will now appear in HACS under the **Integrations** section. Click **Install**.
6. Restart Home Assistant.

---

### Method 2: Manual Installation

1. Copy the `hsem` folder to your `custom_components` folder in your Home Assistant configuration.
2. Restart Home Assistant.
3. Add the integration via the Home Assistant integrations page and configure your settings.

---
