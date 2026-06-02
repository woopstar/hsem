# HSEM Energy Accounting — Mathematical Reference

This document defines the physical energy flow model used by the HSEM planner.
Every equation here corresponds to a constraint enforced in the SoC simulation,
the cost function, and the MILP solver.

---

## Per-slot energy balance

For every planning slot, energy balance must hold:

$$ \text{net_load}[t] = \text{house_load}[t] + \text{ev_planned_load}[t] - \text{pv}[t] $$

When EV integration is disabled, $\text{ev_planned_load}[t] = 0$.

Positive $\text{net_load}[t]$ means the house needs energy (import or battery discharge).
Negative $\text{net_load}[t]$ means there is surplus energy (export or battery charge).

---

## Grid and battery flows

### Source-sink separation

The battery and grid flows must satisfy:

$$ \text{house_load}[t] = \text{pv_to_house}[t] + \text{battery_to_house}[t] + \text{grid_to_house}[t] $$

$$ \text{grid_import}[t] = \text{grid_to_house}[t] + \text{grid_to_battery}[t] + \text{ev_grid_import}[t] $$

### PV production split

$$ \text{pv}[t] = \text{pv_to_house}[t] + \text{pv_to_ev}[t] + \text{pv_to_battery}[t] + \text{pv_exported}[t] + \text{pv_curtailed}[t] $$

### Battery charge

$$ \text{battery_charge_stored}[t] = (\text{pv_to_battery}[t] + \text{grid_to_battery}[t]) \cdot \eta_{chg} $$

Where $\eta_{chg} = \text{charge_efficiency_pct} / 100$.

### Grid import for charging

$$ \text{grid_to_battery}[t] = \text{battery_charge_stored}[t] / \eta_{chg} $$

> **Key invariant:** The cost function prices $\text{grid_to_battery}[t]$, not
> $\text{battery_charge_stored}[t]$. This ensures conversion losses are included
> in the import cost.

### Battery discharge

$$ \text{usable_discharge}[t] = \text{battery_removed}[t] \cdot \eta_{dis} $$

Where $\eta_{dis} = \text{discharge_efficiency_pct} / 100$.

The battery energy removed to supply a target house load:

$$ \text{battery_removed}[t] = \text{house_load_from_battery}[t] / \eta_{dis} $$

---

## SoC forward simulation

For each slot:

$$ \text{soc_after_kwh}[t] = \text{soc_before_kwh}[t] + \text{charge_stored}[t] - \text{battery_removed}[t] $$

### SoC bounds

$$ \text{soc_after_kwh}[t] \in [\text{min_soc_kwh}, \text{max_soc_kwh}] $$

Where:

$$ \text{min_soc_kwh} = \text{rated_kwh} \cdot \frac{\text{end_of_discharge_soc_pct}}{100} $$

$$ \text{max_soc_kwh} = \text{rated_kwh} \cdot \frac{\text{battery_max_soc_pct}}{100} $$

$$ \text{usable_kwh} = \text{max_soc_kwh} - \text{min_soc_kwh} $$

### Power limits (per-slot energy caps)

$$ \text{charge_stored}[t] \leq \text{max_charge_per_slot} = \frac{\text{max_charge_power_w}}{1000} \cdot \frac{\text{interval_minutes}}{60} $$

$$ \text{battery_removed}[t] \leq \text{max_discharge_per_slot} = \frac{\text{max_discharge_power_w}}{1000} \cdot \frac{\text{interval_minutes}}{60} $$

When $\text{max_discharge_power_w}$ is `None` (unlimited), the per-slot cap is
relaxed to $\text{usable_kwh}$.

---

## EV charger energy model

### AC appliance model

The EV charger draws from the **AC bus** — it never draws from the house battery:

$$ \text{ev_ac_load}[t] = \frac{\text{ev_battery_charged}[t]}{\text{charger_efficiency}} $$

Where $\text{charger_efficiency} = \text{charger_efficiency_pct} / 100$.

### Three-field EV load model

| Field | Formula | Meaning |
|---|---|---|
| `ev_planned_load_kwh` | Sum of EV AC loads NOT in house load | Added to net consumption |
| `ev_accounted_load_kwh` | Sum of EV AC loads already in house load | NOT added to net consumption |
| `ev_total_planned_load_kwh` | `ev_planned + ev_accounted` | Total EV activity (diagnostics) |

### Net surplus filtering

The EV planner selects slots using net consumption **after house load**:

$$ \text{slot_net_surplus}[t] = \max(-\text{estimated_net_consumption}[t], 0) $$

Where $\text{estimated_net_consumption}[t] = \text{house_load}[t] - \text{pv}[t]$.

#### Historical note (PR #397, #406)

Before PR #406, the EV planner incorrectly used `estimated_net_consumption` which
was `0.0` at EV planning time (net consumption had not been populated yet). This
meant every slot appeared to have zero surplus, so the EV was always scheduled
as grid-import.

PR #397 fixed this by deriving surplus directly from raw PV and house load
fields. PR #406 improved it further by running `populate_net_consumption`
before EV planning so that PV confidence decay is automatically applied.

---

## Round-trip efficiency

$$ \eta_{roundtrip} = \eta_{chg} \cdot \eta_{dis} $$

$$ \text{roundtrip_loss} = 1 - \eta_{roundtrip} $$

### Example

With 97 % charge and 97 % discharge efficiency:

$$ \eta_{roundtrip} = 0.97 \cdot 0.97 = 0.9409 $$

$$ \text{loss} = 1 - 0.9409 = 0.0591 \text{ (5.91 %)} $$

Charging 10 kWh from the grid:
- Grid draws: $10 / 0.97 = 10.31$ kWh
- Battery stores: 10 kWh
- Discharging 10 kWh battery energy:
- House receives: $10 \cdot 0.97 = 9.7$ kWh
- Net round-trip: 10.31 kWh grid → 9.7 kWh house = 5.91 % loss

---

## PV confidence decay

For multi-day horizons, PV estimates are discounted:

| Day offset | Decay factor |
|---|---|
| 0 (today) | 1.00 |
| 1 (tomorrow) | 0.90 |
| 2 (day after) | 0.80 |

$$ \text{pv_decayed}[t] = \text{pv_raw}[t] \cdot \text{decay_factor}[day\_offset] $$

Prices are **not** decayed because spot-market prices are typically firm by mid-day.

---

## Energy unit conventions

| Conversion | Formula |
|---|---|
| W → kW | $\text{kW} = \text{W} / 1000$ |
| Wh → kWh | $\text{kWh} = \text{Wh} / 1000$ |
| Power → energy | $\text{kWh} = \text{kW} \times \text{hours}$ |
| Accumulated energy | $\text{kWh} = \text{power_W} \times \frac{\text{elapsed_seconds}}{3600} / 1000$ |

All internal planner calculations use **kWh** for energy and **kW** for power.
Power limits from Huawei Solar are received in **Watts** and converted at the
planner boundary.
