# HSEM Energy Accounting — Mathematical Reference

This document defines the physical energy flow model used by the HSEM planner.
Every equation here corresponds to a constraint enforced in the SoC simulation,
the cost function, and the MILP solver.

---

## Per-slot energy balance

For every planning slot, energy balance must hold:

$$
net\\_load[t] = house\\_load[t] + ev\\_planned\\_load[t] - \mathrm{pv}[t]
$$

When EV integration is disabled, $ev\\_planned\\_load[t] = 0$.

Positive $net\\_load[t]$ means the house needs energy (import or battery discharge).
Negative $net\\_load[t]$ means there is surplus energy (export or battery charge).

---

## Grid and battery flows

### Source-sink separation

The battery and grid flows must satisfy:

$$
house\\_load[t] = pv\\_to\\_house[t] + battery\\_to\\_house[t] + grid\\_to\\_house[t]
$$

$$
grid\\_import[t] = grid\\_to\\_house[t] + grid\\_to\\_battery[t] + ev\\_grid\\_import[t]
$$

### PV production split

$$
\mathrm{pv}[t] = pv\\_to\\_house[t] + pv\\_to\\_ev[t] + pv\\_to\\_battery[t] + pv\\_exported[t] + pv\\_curtailed[t]
$$

### Battery charge

$$
battery\\_charge\\_stored[t] = (pv\\_to\\_battery[t] + grid\\_to\\_battery[t]) \cdot \eta_{chg}
$$

Where $\eta_{chg} = charge\\_efficiency\\_pct / 100$.

### Grid import for charging

$$
grid\\_to\\_battery[t] = battery\\_charge\\_stored[t] / \eta_{chg}
$$

> **Key invariant:** The cost function prices $grid\\_to\\_battery[t]$, not
> $battery\\_charge\\_stored[t]$. This ensures conversion losses are included
> in the import cost.

### Battery discharge

$$
usable\\_discharge[t] = battery\\_removed[t] \cdot \eta_{dis}
$$

Where $\eta_{dis} = discharge\\_efficiency\\_pct / 100$.

The battery energy removed to supply a target house load:

$$
battery\\_removed[t] = house\\_load\\_from\\_battery[t] / \eta_{dis}
$$

---

## SoC forward simulation

For each slot:

$$
soc\\_after\\_kwh[t] = soc\\_before\\_kwh[t] + charge\\_stored[t] - battery\\_removed[t]
$$

### SoC bounds

$$
soc\\_after\\_kwh[t] \in [min\\_soc\\_kwh, max\\_soc\\_kwh]
$$

Where:

$$
min\\_soc\\_kwh = rated\\_kwh \cdot \frac{end\\_of\\_discharge\\_soc\\_pct}{100}
$$

$$
max\\_soc\\_kwh = rated\\_kwh \cdot \frac{battery\\_max\\_soc\\_pct}{100}
$$

$$
usable\\_kwh = max\\_soc\\_kwh - min\\_soc\\_kwh
$$

### Power limits (per-slot energy caps)

$$
charge\\_stored[t] \leq max\\_charge\\_per\\_slot = \frac{max\\_charge\\_power\\_w}{1000} \cdot \frac{interval\\_minutes}{60}
$$

$$
battery\\_removed[t] \leq max\\_discharge\\_per\\_slot = \frac{max\\_discharge\\_power\\_w}{1000} \cdot \frac{interval\\_minutes}{60}
$$

When $max\\_discharge\\_power\\_w$ is `None` (unlimited), the per-slot cap is
relaxed to $usable\\_kwh$.

---

## EV charger energy model

### AC appliance model

The EV charger draws from the **AC bus** — it never draws from the house battery:

$$
ev\\_ac\\_load[t] = \frac{ev\\_battery\\_charged[t]}{charger\\_efficiency}
$$

Where $charger\\_efficiency = charger\\_efficiency\\_pct / 100$.

### Three-field EV load model

| Field                       | Formula                                                           | Meaning                                                       |
| --------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------- |
| `ev_planned_load_kwh`       | EV AC contributions not embedded in the normalized house baseline | Added to net consumption                                      |
| `ev_accounted_load_kwh`     | EV AC contributions still embedded in the normalized baseline     | Subtracted once to recover pure-house demand; not added again |
| `ev_total_planned_load_kwh` | `ev_planned + ev_accounted`                                       | Total EV activity (diagnostics)                               |

The raw CT position and normalized baseline are separate contracts. HSEM history
preprocessing removes each EV with configured power telemetry, and accepted
current-slot live normalization records removal per EV. Mixed two-EV accounting
is valid; missing telemetry is not equivalent to a genuine 0 W reading.

### Net surplus filtering

The EV planner selects slots using net consumption **after house load**:

$$
slot\\_net\\_surplus[t] = \max(-estimated\\_net\\_consumption[t], 0)
$$

Where $estimated\\_net\\_consumption[t] = house\\_load[t] - \mathrm{pv}[t]$.

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

$$
\eta_{roundtrip} = \eta_{chg} \cdot \eta_{dis}
$$

$$
roundtrip\\_loss = 1 - \eta_{roundtrip}
$$

### Example

With 97 % charge and 97 % discharge efficiency:

$$
\eta_{roundtrip} = 0.97 \cdot 0.97 = 0.9409
$$

$$
\mathrm{loss} = 1 - 0.9409 = 0.0591 \ (5.91\\,\\%)
$$

Charging 10 kWh from the grid:

- Grid draws: $10 / 0.97 = 10.31$ kWh
- Battery stores: 10 kWh
- Discharging 10 kWh battery energy:
- House receives: $10 \cdot 0.97 = 9.7$ kWh
- Net round-trip: 10.31 kWh grid → 9.7 kWh house = 5.91 % loss

### Measured efficiency

HSEM also measures the efficiency from the battery power sensor and the SoC
(issue #1431), per 500 W power step and per direction. Both are ratios of
summed energies:

$$
\eta_{chg,apparent} = \frac{\sum \Delta soc / 100 \cdot rated\\_kwh}{\sum energy\\_at\\_sensor}
$$

$$
\eta_{dis,apparent} = \frac{\sum energy\\_at\\_sensor}{\sum \Delta soc / 100 \cdot rated\\_kwh}
$$

The two are _apparent_: the stored energy is computed from the rated capacity,
so a battery that really holds $k \cdot rated\\_kwh$ reads $\eta_{chg} / k$
and $\eta_{dis} \cdot k$. The factor $k$ cancels in the product, so the
measured round trip does not depend on the capacity:

$$
\eta_{rt,measured} = \eta_{chg,apparent} \cdot \eta_{dis,apparent}
$$

The values are published on `sensor.hsem_battery_efficiency_sensor`.

The sensor reads at the battery, on the DC side of the inverter. The inverter's
conversion loss is not in $\eta_{rt,measured}$, so the round trip the grid
meter sees is lower:

$$
\eta_{rt,meter} \le \eta_{rt,measured}
$$

The planner keeps the configured $\eta_{chg}$ and $\eta_{dis}$ unless
`hsem_batteries_learned_efficiency_enabled` is on (default off). Then, when
the measurement has 40 samples per direction, is at most 30 days old and
$0.70 \le \eta_{rt,measured} < \eta_{chg} \cdot \eta_{dis}$, both configured
values are scaled by the same factor:

$$
s = \sqrt{\frac{\eta_{rt,measured}}{\eta_{chg} \cdot \eta_{dis}}}, \qquad
\eta_{chg,plan} = s \cdot \eta_{chg}, \qquad
\eta_{dis,plan} = s \cdot \eta_{dis}
$$

Only the round trip is used, because the two apparent figures carry the
capacity factor $k$. A measured round trip can only lower the planner's
efficiency, because it is an upper bound of the meter-side one. The plan cost
then still leaves out the inverter loss. See `docs/planner-spec.md` →
_Planning with the measured round trip_.

The energy accounting above always uses the configured values.

---

## PV confidence decay

For multi-day horizons, PV estimates are discounted:

| Day offset    | Decay factor |
| ------------- | ------------ |
| 0 (today)     | 1.00         |
| 1 (tomorrow)  | 0.90         |
| 2 (day after) | 0.80         |

$$
pv\\_decayed[t] = pv\\_raw[t] \cdot decay\\_factor[day\\_offset]
$$

Prices are **not** decayed because spot-market prices are typically firm by mid-day.

---

## Energy unit conventions

| Conversion         | Formula                                                                 |
| ------------------ | ----------------------------------------------------------------------- |
| W → kW             | $\mathrm{kW} = \mathrm{W} / 1000$                                       |
| Wh → kWh           | $\mathrm{kWh} = \mathrm{Wh} / 1000$                                     |
| Power → energy     | $\mathrm{kWh} = \mathrm{kW} \times \mathrm{hours}$                      |
| Accumulated energy | $\mathrm{kWh} = power\\_W \times \frac{elapsed\\_seconds}{3600} / 1000$ |

All internal planner calculations use **kWh** for energy and **kW** for power.
Power limits from Huawei Solar are received in **Watts** and converted at the
planner boundary.
