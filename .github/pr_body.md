## Summary

Implements automatic excess battery export at peak export prices using the `huawei_solar.set_forcible_discharge` API. When the battery is predicted to hold more charge than needed to cover overnight/morning consumption, HSEM will schedule a forcible discharge during the highest-priced export windows.

---

## Changes

### New Feature

- **`flows/batteries_excess_export.py`** - New config-flow step that collects:
  - Enable/disable toggle for excess export
  - Export discharge buffer (% SOC to keep in reserve)
  - Grid-charge price threshold (minimum spread before discharging grid-charged energy)
- **`utils/huawei.py`** - `async_apply_excess_battery_export()` calls `set_forcible_discharge` for each qualifying export hour, sorted by export price descending.
- **`utils/misc.py`** - `calculate_recommended_threshold()` helper calculates minimum viable export price from purchase price, cycle count, and conversion loss.
- **`custom_sensors/working_mode_sensor.py`** - `_async_apply_excess_battery_export()` integrates excess export logic into the hourly working-mode calculation.
- **`translations/en.json`** - UI strings for the new config step.

### Bug Fixes (identified during review)

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `working_mode_sensor.py` | Double buffer application — buffer was applied as a kWh addition **and** as a SOC multiplier, exporting ~37% less than intended | Removed redundant SOC multiplier; buffer is already embedded in required-battery calculation |
| 2 | `const.py` | `hsem_batteries_excess_export_discharge_buffer` defaulted to `False` instead of `10`; `hsem_batteries_excess_export_price_threshold` defaulted to `False` instead of `0.10` — caused runtime type errors | Changed defaults to correct numeric values |
| 3 | `working_mode_sensor.py` | SOC floor was `max(0, ...)` — battery could be driven to 0% if no excess solar found | Changed to `max(10, ...)` to enforce minimum 10% SOC reserve |
| 4 | `flows/batteries_schedule_1/2/3.py`, `flows/batteries_excess_export.py`, `config_flow.py` | Config flow steps called `get_config_value(None, ...)` — values from earlier steps (purchase price, cycles, conversion loss) were never read when calculating recommended threshold | Added `user_input: dict or None` parameter; lookup priority: `config_entry > user_input > defaults`; all 4 step callers now pass `self._user_input` |
| 5 | `config_flow.py` | `async_step_batteries_schedule_3` called itself on success instead of advancing to `async_step_batteries_excess_export` | Fixed recursive call to advance to correct next step |

### Housekeeping

- `.gitignore` - added `.tox` directory; removed tracked `.tox/py311/.tox-info.json` artefact from index.

---

## Testing Checklist

- [ ] Normal operation — buffer applied once; target SOC matches required battery (no double-buffer)
- [ ] No excess solar in forecast — safety floor holds at >= 10% SOC
- [ ] Solar-charged battery — discharges at any positive export price
- [ ] Grid-charged battery — only discharges when `(export_price - import_price) >= threshold`
- [ ] Config flow — purchase price / cycles / conversion loss entered in step 1 pre-populate recommended threshold in excess-export step
- [ ] Options flow — existing config values populate form correctly
