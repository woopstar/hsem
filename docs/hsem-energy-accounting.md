# HSEM Energy Accounting — Mathematical Reference

This document defines the physical energy flow model used by the HSEM planner.
Every equation here corresponds to a constraint enforced in the SoC simulation,
the cost function, and the MILP solver.

---

## Per-slot energy balance

For every planning slot, energy balance must hold:

$$ \text{net\_load}[t] = \text{house\_load}[t] + \text{ev\_planned\_load}[t] - \text{pv}[t] $$

When EV integration is disabled, $\text{ev\_planned\_load}[t] = 0$.

Positive $\text{net\_load}[t]$ means the house needs energy (import or battery discharge).
Negative $\text{net\_load}[t]$ means there is surplus energy (export or battery charge).

---

## Grid and battery flows

### Source-sink separation

The battery and grid flows must satisfy:

$$ \text{house\_load}[t] = \text{pv\_to\_house}[t] + \text{battery\_to\_house}[t] + \text{grid\_to\_house}[t] $$

$$ \text{grid\_import}[t] = \text{grid\_to\_house}[t] + \text{grid\_to\_battery}[t] + \text{ev\_grid\_import}[t] $$

### PV production split

$$ \text{pv}[t] = \text{pv\_to\_house}[t] + \text{pv\_to\_ev}[t] + \text{pv\_to\_battery}[t] + \text{pv\_exported}[t] + \text{pv\_curtailed}[t] $$

### Battery charge

$$ \text{battery\_charge\_stored}[t] = (\text{pv\_to\_battery}[t] + \text{grid\_to\_battery}[t]) \cdot \eta_{chg} $$

Where $\eta_{chg} = \text{charge\_efficiency\_pct} / 100$.

### Grid import for charging

$$ \text{grid\_to\_battery}[t] = \text{battery\_charge\_stored}[t] / \eta_{chg} $$

> **Key invariant:** The cost function prices $\text{grid\_to\_battery}[t]$, not
> $\text{battery\_charge\_stored}[t]$. This ensures conversion losses are included
> in the import cost.

### Battery discharge

$$ \text{usable\_discharge}[t] = \text{battery\_removed}[t] \cdot \eta_{dis} $$

Where $\eta_{dis} = \text{discharge\_efficiency\_pct} / 100$.

The battery energy removed to supply a target house load:

$$ \text{battery\_removed}[t] = \text{house\_load\_from\_battery}[t] / \eta_{dis} $$

---

## SoC forward simulation

For each slot:

$$ \text{soc\_after\_kwh}[t] = \text{soc\_before\_kwh}[t] + \text{charge\_stored}[t] - \text{battery\_removed}[t] $$

### SoC bounds

$$ \text{soc\_after\_kwh}[t] \in [\text{min\_soc\_kwh}, \text{max\_soc\_kwh}] $$

Where:

$$ \text{min\_soc\_kwh} = \text{rated\_kwh} \cdot \frac{\text{end\_of\_discharge\_soc\_pct}}{100} $$

$$ \text{max\_soc\_kwh} = \text{rated\_kwh} \cdot \frac{\text{battery\_max\_soc\_pct}}{100} $$

$$ \text{usable\_kwh} = \text{max\_soc\_kwh} - \text{min\_soc\_kwh} $$

### Power limits (per-slot energy caps)

$$ \text{charge\_stored}[t] \leq \text{max\_charge\_per\_slot} = \frac{\text{max\_charge\_power\_w}}{1000} \cdot \frac{\text{interval\_minutes}}{60} $$

$$ \text{battery\_removed}[t] \leq \text{max\_discharge\_per\_slot} = \frac{\text{max\_discharge\_power\_w}}{1000} \cdot \frac{\text{interval\_minutes}}{60} $$

When $\text{max\_discharge\_power\_w}$ is `None` (unlimited), the per-slot cap is
relaxed to $\text{usable\_kwh}$.

---

## EV charger energy model

### AC appliance model

The EV charger draws from the **AC bus** — it never draws from the house battery:

$$ \text{ev\_ac\_load}[t] = \frac{\text{ev\_battery\_charged}[t]}{\text{charger\_efficiency}} $$

Where $\text{charger\_efficiency} = \text{charger\_efficiency\_pct} / 100$.

### Three-field EV load model

| Field | Formula | Meaning |
|---|---|---|
| `ev_planned_load_kwh` | Sum of EV AC loads NOT in house load | Added to net consumption |
| `ev_accounted_load_kwh` | Sum of EV AC loads already in house load | NOT added to net consumption |
| `ev_total_planned_load_kwh` | `ev_planned + ev_accounted` | Total EV activity (diagnostics) |

### Net surplus filtering

The EV planner selects slots using net consumption **after house load**:

$$ \text{slot\_net\_surplus}[t] = \max(-\text{estimated\_net\_consumption}[t], 0) $$

Where $\text{estimated\_net\_consumption}[t] = \text{house\_load}[t] - \text{pv}[t]$.

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

$$ \text{roundtrip\_loss} = 1 - \eta_{roundtrip} $$

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

$$ \text{pv\_decayed}[t] = \text{pv\_raw}[t] \cdot \text{decay\_factor}[day\_offset] $$

Prices are **not** decayed because spot-market prices are typically firm by mid-day.

---

## Energy unit conventions

| Conversion | Formula |
|---|---|
| W → kW | $\text{kW} = \text{W} / 1000$ |
| Wh → kWh | $\text{kWh} = \text{Wh} / 1000$ |
| Power → energy | $\text{kWh} = \text{kW} \times \text{hours}$ |
| Accumulated energy | $\text{kWh} = \text{power\_W} \times \frac{\text{elapsed\_seconds}}{3600} / 1000$ |

All internal planner calculations use **kWh** for energy and **kW** for power.
Power limits from Huawei Solar are received in **Watts** and converted at the
planner boundary.
