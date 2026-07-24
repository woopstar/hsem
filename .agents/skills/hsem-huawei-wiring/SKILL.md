---
name: hsem-huawei-wiring
description: Activate when adding, modifying, or using any Huawei Solar inverter/battery sensor entity in HSEM. Follow the full wiring protocol from const.py through coordinator.py.
---

# HSEM Huawei Solar Sensor Wiring

Activate this skill when you need to:
- Add a new Huawei Solar entity to HSEM
- Use an existing Huawei Solar entity value
- Reference battery/inverter parameters (max SoC, charge cutoff, rated capacity, etc.)

## Golden Rule

**Every hardware value consumed or written by HSEM MUST use the entity exposed by the [`wlcrs/huawei_solar`](https://github.com/wlcrs/huawei_solar) Home Assistant integration.** Never hard-code numeric battery constants — always source from the live HA entity.

## Step 1: Check `docs/huawei_entities.md` First

This is the canonical, verified list of every entity exposed by the `wlcrs/huawei_solar` integration on this installation. Only fall back to searching the upstream repo when an entity is not yet listed there.

## Step 2: If Entity Already Exists in HSEM

Re-use it. Do not hard-code the value. The entity should already be wired through `flows/huawei_solar.py`, `sensor_config.py`, `config_reader.py`, `state_collector.py`, and `live_state.py`.

## Step 3: If Entity Exists in `wlcrs/huawei_solar` But NOT Yet Wired Into HSEM

Add it through the **full stack in this exact order**:

1. **`const.py`** — Add a default entity-id string under `DEFAULT_CONFIG_VALUES`
2. **`flows/huawei_solar.py`** — Add to the schema and validation
3. **`translations/en.json`** — Add `data` label and `data_description` for the new field in **both** `config.step.huawei_solar` and `options.step.huawei_solar`
4. **`models/sensor_config.py`** — Add the `str | None` field
5. **`custom_sensors/config_reader.py`** — Read from config entry
6. **`custom_sensors/state_collector.py`** — Read the HA entity state
7. **`models/live_state.py`** — Add the field to `LiveState`
8. **`coordinator.py`** — Pass to `PlannerInput` (if planner-relevant)

## Step 4: If Entity Is New to `docs/huawei_entities.md`

Add it to `docs/huawei_entities.md` as part of the same PR that wires it into HSEM.

## Key Entity Mappings

| Register / Source | Entity | Meaning |
|---|---|---|
| `STORAGE_CHARGING_CUTOFF_CAPACITY` | `number.batteries_end_of_charge_soc` | Max SoC during charging (90-100 %) |
| `STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE` | `number.batteries_grid_charge_cutoff_soc` | Max SoC when charging from grid |
| `STORAGE_DISCHARGING_CUTOFF_CAPACITY` | `number.batteries_end_of_discharge_soc` | Min SoC floor |
| `STORAGE_MAXIMUM_CHARGING_POWER` | `number.batteries_maximum_charging_power` | Max charge power (W) |
| `STORAGE_MAXIMUM_DISCHARGING_POWER` | `number.batteries_maximum_discharging_power` | Max discharge power (W) |
| `STORAGE_STATE_OF_CAPACITY` | `sensor.batteries_state_of_capacity` | Current SoC (%) |
| `STORAGE_RATED_CAPACITY` | `sensor.batteries_rated_capacity` | Nameplate capacity (Wh) |
| `STORAGE_WORKING_MODE_SETTINGS` | `select.batteries_working_mode` | Working mode select |
| `STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU` | `select.batteries_excess_pv_energy_use_in_tou` | Excess PV use mode in TOU |
| `STORAGE_HUAWEI_LUNA2000_TOU_…_PERIODS` | `sensor.batteries_tou_charging_and_discharging_periods` | TOU period schedule |

## Never Do This

- Never use a fixed numeric constant for a value that the inverter reports (max SoC, charge cutoff, rated capacity)
- Never guess an entity ID — check `docs/huawei_entities.md` first
- Never skip a step in the wiring stack
- Never forget to update `translations/en.json` for both `config` and `options` steps
