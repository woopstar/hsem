# Huawei Solar — Available HA Entities

> **Canonical reference for all AI agents and developers.**
> Before using any battery, inverter, or power-meter value in HSEM, look up the correct
> entity ID here. Do **not** guess or invent entity IDs — use only what appears in this file.
>
> This file reflects the actual entities exposed by the `wlcrs/huawei_solar` integration on
> this installation. Update it whenever the integration or hardware changes.

---

## Batteries

### number entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| End-of-charge SOC | `number.batteries_end_of_charge_soc` | % | ✅ `hsem_huawei_solar_batteries_charging_cutoff_capacity` |
| End-of-discharge SOC | `number.batteries_end_of_discharge_soc` | % | ✅ `hsem_huawei_solar_batteries_end_of_discharge_soc` |
| Grid charge cutoff SOC | `number.batteries_grid_charge_cutoff_soc` | % | ✅ `hsem_huawei_solar_batteries_grid_charge_cutoff_soc` |
| Grid charge maximum power | `number.batteries_grid_charge_maximum_power` | W | — |
| Maximum charging power | `number.batteries_maximum_charging_power` | W | ✅ `hsem_huawei_solar_batteries_maximum_charging_power` |
| Maximum discharging power | `number.batteries_maximum_discharging_power` | W | ✅ `hsem_huawei_solar_batteries_maximum_discharging_power` |
| Peak Shaving SOC | `number.batteries_peak_shaving_soc` | % | — |

### sensor entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| State of capacity (SoC) | `sensor.batteries_state_of_capacity` | % | ✅ `hsem_huawei_solar_batteries_state_of_capacity` |
| Rated capacity | `sensor.batteries_rated_capacity` | Wh | ✅ `hsem_huawei_solar_batteries_rated_capacity` |
| TOU charging and discharging periods | `sensor.batteries_tou_charging_and_discharging_periods` | — | ✅ `hsem_huawei_solar_batteries_tou_charging_and_discharging_periods` |

### select entities

| Friendly name | Entity ID | Used by HSEM |
|---|---|---|
| Working mode | `select.batteries_working_mode` | ✅ `hsem_huawei_solar_batteries_working_mode` |
| Excess PV energy use in TOU | `select.batteries_excess_pv_energy_use_in_tou` | ✅ `hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou` |

---

## Inverter

### sensor entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| Active power control | `sensor.inverter_active_power_control` | — | ✅ `hsem_huawei_solar_inverter_active_power_control` (optional — not available on EMMA installations) |
| Locking status | `sensor.inverter_locking_status` | — | — |
| Max active power | `sensor.inverter_max_active_power` | W | — |
| Monthly yield | `sensor.inverter_monthly_yield` | kWh | — |
| Off-grid status | `sensor.inverter_off_grid_status` | — | — |
| Off-grid switch | `sensor.inverter_off_grid_switch` | — | — |
| Phase A current | `sensor.inverter_phase_a_current` | A | — |
| Phase A voltage | `sensor.inverter_phase_a_voltage` | V | — |
| Phase B current | `sensor.inverter_phase_b_current` | A | — |
| Phase B voltage | `sensor.inverter_phase_b_voltage` | V | — |
| Phase C current | `sensor.inverter_phase_c_current` | A | — |
| Phase C voltage | `sensor.inverter_phase_c_voltage` | V | — |
| Power factor | `sensor.inverter_power_factor` | — | — |
| PV 1 current | `sensor.inverter_pv_1_current` | A | — |
| PV 1 voltage | `sensor.inverter_pv_1_voltage` | V | — |
| PV 2 current | `sensor.inverter_pv_2_current` | A | — |
| PV 2 voltage | `sensor.inverter_pv_2_voltage` | V | — |
| PV connection status | `sensor.inverter_pv_connection_status` | — | — |
| Rated power | `sensor.inverter_rated_power` | W | — |
| Reactive power | `sensor.inverter_reactive_power` | var | — |
| Shutdown time | `sensor.inverter_shutdown_time` | — | — |
| Startup time | `sensor.inverter_startup_time` | — | — |
| State | `sensor.inverter_inverter_state` | — | — |
| Total DC input energy | `sensor.inverter_total_dc_input_energy` | kWh | — |
| Total yield | `sensor.inverter_total_yield` | kWh | — |
| Yearly yield | `sensor.inverter_yearly_yield` | kWh | — |

### number entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| MPPT-Scan Interval | `number.inverter_mppt_scan_interval` | min | — |
| Power derating | `number.inverter_power_derating` | W | — |
| Power derating (by percentage) | `number.inverter_power_derating_by_percentage` | % | — |

### switch entities

| Friendly name | Entity ID | Used by HSEM |
|---|---|---|
| MPPT-Scan | `switch.inverter_mppt_scanning` | — |

---

## Power Meter

### sensor entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| A-B line voltage | `sensor.power_meter_a_b_line_voltage` | V | — |
| Active power | `sensor.power_meter_active_power` | W | — |
| B-C line voltage | `sensor.power_meter_b_c_line_voltage` | V | — |
| C-A line voltage | `sensor.power_meter_c_a_line_voltage` | V | — |
| Consumption | `sensor.power_meter_consumption` | kWh | — |
| Exported | `sensor.power_meter_exported` | kWh | — |
| Frequency | `sensor.power_meter_frequency` | Hz | — |
| Meter status | `sensor.power_meter_meter_status` | — | — |
| Phase A active power | `sensor.power_meter_phase_a_active_power` | W | — |
| Phase A current | `sensor.power_meter_current` | A | — |
| Phase A voltage | `sensor.power_meter_phase_a_voltage` | V | — |
| Phase B active power | `sensor.power_meter_phase_b_active_power` | W | — |
| Phase B current | `sensor.power_meter_current_2` | A | — |
| Phase B voltage | `sensor.power_meter_phase_b_voltage` | V | — |
| Phase C active power | `sensor.power_meter_phase_c_active_power` | W | — |
| Phase C current | `sensor.power_meter_current_3` | A | — |
| Phase C voltage | `sensor.power_meter_phase_c_voltage` | V | — |
| Power factor | `sensor.power_meter_power_factor` | — | — |
| Reactive energy | `sensor.power_meter_reactive_energy` | kvarh | — |
| Reactive power | `sensor.power_meter_reactive_power` | var | — |

---

## EMMA

In installations where an EMMA (Energy Management Assistant) is the primary device, the inverter is subordinate to EMMA.  Active power control is managed by EMMA rather than the inverter, so `sensor.inverter_active_power_control` **does not exist** on EMMA-based setups.

### number entities

| Friendly name | Entity ID | Unit | Used by HSEM |
|---|---|---|---|
| Maximum feed grid power (%) | `number.emma_*_maximum_feed_grid_power_percent` | % | ✅ `hsem_huawei_solar_emma_active_power_control` |
| Maximum feed grid power (W) | `number.emma_*_maximum_feed_grid_power_watt` | W | — |

> **Note**: HSEM now supports EMMA-based installations.  Configure your EMMA device (`hsem_huawei_solar_device_id_emma`) and the maximum feed grid power percentage number entity (`hsem_huawei_solar_emma_active_power_control`) in the config flow, and HSEM will route export control service calls to the EMMA device automatically.
