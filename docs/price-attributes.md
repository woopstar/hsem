# Price Attributes: Published Prices or Predictions

HSEM reads prices from the attributes of the configured price sensors. It keeps
two kinds of price apart (issue #1434):

- A **published** price is what the market or the retailer has set for a slot.
  HSEM records it in the price history and holds it for a price sensor outage.
- A **prediction** is a model's guess. It prices a slot like any other value.
  It is never recorded and never held.

The same attribute name holds published prices in one integration and
predictions in another. This page lists what each supported integration puts
in each attribute, with the source that was read (issue #1452, read on
2026-11-21).

## What each integration publishes

| Integration                      | Entity                                                                   | Attribute                               | Entry keys                               | Content                                                                                  | Source                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------ | --------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Strømligning                     | Today sensors (`current_price_*`, `spotprice_*`)                         | `prices`                                | `start`, `price`                         | Published                                                                                | [sensor.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/sensor.py#L551-L597), [base.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/base.py#L59-L78)                                                                    |
| Strømligning                     | Tomorrow binary sensors (`tomorrow_available_*`, `tomorrow_spotprice_*`) | `prices`                                | `start`, `price`                         | Published. **Predictions** while tomorrow is not published and the forecast option is on | [api.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/api.py#L254-L265), [binary_sensor.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/binary_sensor.py#L135-L144)                                                      |
| Strømligning                     | Forecast sensors (`forecasts_*`, optional)                               | `prices`                                | `start`, `price`                         | **Predictions**, from the day after tomorrow                                             | [sensor.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/sensor.py#L567-L582), [api.py](https://github.com/MTrab/stromligning/blob/ef3ab1751f1d5636a5af9b7fdf7d73f9213509bc/custom_components/stromligning/api.py#L246-L248)                                                                    |
| Open Spot Forecast               | Predbat rate entities (`*_predbat_{import,export}_{today,tomorrow}`)     | `prices_today`, `prices_tomorrow`       | `start`, `price`                         | **Mixed**: confirmed prices first, then the ML forecast                                  | [predbat_sensor.py](https://github.com/woopstar/open_spot_forecast/blob/6f8ef04e8a683635ec4fa26cef1d972fc3395721/custom_components/open_spot_forecast/predbat_sensor.py#L174-L236), [spot_prices.py](https://github.com/woopstar/open_spot_forecast/blob/6f8ef04e8a683635ec4fa26cef1d972fc3395721/custom_components/open_spot_forecast/spot_prices.py#L126-L170)      |
| Open Spot Forecast               | Price Forecast (ML)                                                      | `predictions`                           | `start`, `price` or `s`, `t`             | **Predictions**. Mixed when `include_known_prices` is on                                 | [sensor.py](https://github.com/woopstar/open_spot_forecast/blob/6f8ef04e8a683635ec4fa26cef1d972fc3395721/custom_components/open_spot_forecast/sensor.py#L559-L610)                                                                                                                                                                                                    |
| Tibber Prices                    | Chart data export                                                        | `data` (default), `prices` (configured) | `start_time`, `price_per_kwh` or `price` | Published                                                                                | [chart_data.py](https://github.com/jpawlowski/hass.tibber_prices/blob/cecbfa97410bc147b827cc76e6527631521e9989/custom_components/tibber_prices/sensor/chart_data.py#L84-L134), [get_chartdata.py](https://github.com/jpawlowski/hass.tibber_prices/blob/cecbfa97410bc147b827cc76e6527631521e9989/custom_components/tibber_prices/services/get_chartdata.py#L318-L326) |
| EPEX Spot, Energyforecast source | Market Price, Total Price                                                | `data`                                  | `start_time`, `price_per_kwh`            | **Mixed**: market prices, then a forecast up to 96 hours ahead                           | [Energyforecast](https://github.com/mampfes/ha_epex_spot/blob/72fb547cdb4cc8bbcf65862c54815fcfd67782db/custom_components/epex_spot/EPEXSpot/Energyforecast/__init__.py#L15-L86), [sensor.py](https://github.com/mampfes/ha_epex_spot/blob/72fb547cdb4cc8bbcf65862c54815fcfd67782db/custom_components/epex_spot/sensor.py#L68-L119)                                    |
| EPEX Spot, other sources         | Market Price, Total Price                                                | `data`                                  | `start_time`, `price_per_kwh`            | Published                                                                                | [const.py](https://github.com/mampfes/ha_epex_spot/blob/72fb547cdb4cc8bbcf65862c54815fcfd67782db/custom_components/epex_spot/const.py#L22-L29)                                                                                                                                                                                                                        |
| ENTSO-e                          | Average price sensor                                                     | `prices_today`, `prices_tomorrow`       | `time`, `price`                          | Published                                                                                | [sensor.py](https://github.com/JaccoR/hass-entso-e/blob/cbdf67ac0e73fd685a02e27ad7b6bef2c5af7ca6/custom_components/entsoe/sensor.py#L267-L279)                                                                                                                                                                                                                        |
| Energi Data Service              | Price sensor                                                             | `raw_today`, `raw_tomorrow`             | `hour`, `price`                          | Published                                                                                | [sensor.py](https://github.com/MTrab/energidataservice/blob/7bbd8d01692dd1a269f16dfc760d4a7aa0d17e68/custom_components/energidataservice/sensor.py#L678-L712)                                                                                                                                                                                                         |
| Energi Data Service              | Price sensor, Carnot enabled                                             | `forecast`                              | `hour`, `price`                          | **Predictions**                                                                          | [sensor.py](https://github.com/MTrab/energidataservice/blob/7bbd8d01692dd1a269f16dfc760d4a7aa0d17e68/custom_components/energidataservice/sensor.py#L832-L841)                                                                                                                                                                                                         |
| Nord Pool (custom)               | Price sensor                                                             | `raw_today`, `raw_tomorrow`             | `start`, `value`                         | Published                                                                                | [sensor.py](https://github.com/custom-components/nordpool/blob/1eaa2490943bb41a2e322e9349ba2d7c6746a94b/custom_components/nordpool/sensor.py#L394-L421)                                                                                                                                                                                                               |
| Amber Electric                   | Forecast sensor                                                          | `forecasts`                             | `start_time`, `per_kwh`                  | **Predictions**                                                                          | [sensor.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/amberelectric/sensor.py#L105-L160)                                                                                                                                                                                                                                               |

Energi Data Service has no `prices_today` attribute. Enever, Pstryk and Elering
EE also publish `prices_today` and `prices_tomorrow` with `time` and `price`.
Their code was skimmed, not read in full, and showed published prices only.

## How HSEM decides

Three of the four attributes `prices`, `prices_today`, `prices_tomorrow` and
`data` hold predictions in at least one integration, and published prices in
others. Moving a whole attribute to the predictions would stop the price
history for every Strømligning, Tibber Prices and EPEX Spot setup. HSEM reads
the sensor's own state instead
(`_published_until` in `custom_sensors/hourly_data_populator/prices_solcast.py`).
The first rule that applies decides:

| Rule | Sensor state                                                | Result                                             | Covers                                                       |
| ---- | ----------------------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------ |
| 0    | The attribute is `raw_today` or `raw_tomorrow`              | Every entry is published                           | Energi Data Service, Nord Pool                               |
| 1    | The attribute is `forecast`, `predictions` or `forecasts`   | Every entry is a prediction                        | Energi Data Service with Carnot, Open Spot Forecast, Amber   |
| 2    | The sensor has `forecast_data: true`                        | Every entry is a prediction                        | Strømligning tomorrow sensors while tomorrow is a forecast   |
| 3    | The sensor has `known_until`                                | An entry that starts before that time is published | Open Spot Forecast Predbat rate entities                     |
| 4    | An entry starts 60 hours or more after local midnight today | Every entry of that attribute is a prediction      | EPEX Spot with Energyforecast, Strømligning forecast sensors |
| 5    | None of the above                                           | Every entry is published                           | Everything else                                              |

- Rule 0: `raw_today` and `raw_tomorrow` are always published prices. Rules
  2 to 4 never apply to them.
- Rule 3 with a `known_until` that cannot be read as a time makes every entry
  a prediction.
- Rule 4: day-ahead prices are published for today and tomorrow, so a series
  that reaches further holds predictions. Nothing on such a sensor says where
  the published prices end, so none of it counts as published. The 60 hours
  are two days plus 12 hours, for a market day in another time zone than the
  local one.
- A prediction prices its slot exactly as before. The only difference is that
  it is not recorded in the price history and not held for an outage. A price
  sensor that publishes predictions only is alive, not in an outage.
- A price forecast sensor (the optional second sensor per side) is never
  recorded or held, whatever its attributes are.

## How to see it

- HSEM logs a warning once when a price sensor publishes predictions only:
  that side gets no price history and no held prices.
- The diagnostics dump and the `hsem.export_diagnostics` response hold
  `price_sensors`, with `published`, `predictions` or `none` for the import
  and the export sensor, as of the last cycle.

## What this means for a setup

- **Strømligning as price sensor, Open Spot Forecast as forecast sensor** (the
  recommended setup): nothing changes. The Strømligning today sensors are
  published prices.
- **EPEX Spot with the Energyforecast source as price sensor**: no price is
  recorded or held. Missing prices use the nearest earlier day, and a price
  outage has no held prices to plan from.
- **An Open Spot Forecast Predbat entity as price sensor**: the prices before
  `known_until` are recorded and held, the forecast after it is not.

## Not verified

- Each source module of EPEX Spot other than Energyforecast was not read. They
  are market data sources by name (Awattar, SMARD, smartENERGY, Tibber,
  ENTSO-E, Energy-Charts, Hofer Grünstrom).
- The Energyforecast API marks each entry as market or forecast
  (`price_origin`). EPEX Spot drops that field. This was seen in other
  projects that use the API, not in the API's own documentation.
- "Tibber Prices" is taken to be `jpawlowski/hass.tibber_prices`, the
  integration whose export can be set to `prices` with `start_time` and
  `price`.
