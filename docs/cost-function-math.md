# HSEM Cost Function — Mathematical Reference

This document provides the complete mathematical formulation of the HSEM cost function
(`planner/cost_function.py`). It is the source of truth for all cost calculations.

---

## Two-aggregate architecture

The cost function returns two distinct aggregates for every plan:

| Aggregate | Symbol | Contents | Used for |
|---|---|---|---|
| **total_cost** | $C_{total}$ | Real money terms only | Auditing, bill comparison |
| **score** | $S$ | $C_{total}$ + synthetic penalties + terminal SoC value | Candidate selection |

The selector picks the plan with the **lowest score**, not the lowest money cost.

---

## Total cost (money terms)

$$ C_{total} = C_{import} - R_{export} + C_{cycle} + C_{loss} + C_{tariff} $$

### Grid import cost

$$ C_{import} = \sum_{t \in slots} gi[t] \cdot p_{imp}[t] $$

Where $gi[t]$ is the actual grid import in kWh and $p_{imp}[t]$ is the import
price in currency/kWh.

> The cost function prices **actual grid energy drawn**, not stored energy.
> If the battery stores $x$ kWh and charge efficiency is $e$, the grid import
> is $x/e$, which includes conversion losses implicitly.

### Export revenue

$$ R_{export} = \sum_{t \in slots} ge[t] \cdot p_{exp}[t] $$

Where $ge[t]$ is the grid export in kWh and $p_{exp}[t]$ is the export price.

> Revenue is subtracted from total cost. Negative export prices (curtailment
> penalties) increase total cost.

### Battery cycle cost (depreciation)

$$ C_{cycle} = \sum_{t \in slots} \max(charge[t], discharge[t]) \cdot c_{cycle} $$

Where $c_{cycle}$ is the cycle cost per kWh throughput.

The cycle cost counts the **maximum** of charge and discharge per slot, not
their sum. This matches the MILP formulation where $m[t] = \max(ec[t], ed[t])$
and the 2× denominator in the cycle cost formula:

$$ c_{cycle} = \frac{purchase\_price}{2 \cdot usable\_kwh \cdot expected\_cycles} $$

The 2× denominator accounts for one full round-trip (charge + discharge = 2 ×
usable_kwh throughput per cycle). With this factor, charging $x$ kWh and
discharging $x$ kWh costs $c_{cycle} \cdot \max(x, x) = c_{cycle} \cdot x$,
which equals $\frac{purchase\_price \cdot x}{2 \cdot usable \cdot cycles}$ —
matching the expected wear for moving $x$ kWh through the battery in one
direction.

### Conversion loss cost

$$ C_{loss} = \sum_{t \in slots} \frac{charge[t] + discharge[t]}{2} \cdot \frac{\eta_{loss}}{100} \cdot \frac{p_{imp}[t] + p_{exp}[t]}{2} $$

Where $\eta_{loss}$ is the round-trip conversion loss percentage.

The conversion loss term prices the energy lost as heat during charge/discharge
at the average of import and export price — an opportunity-cost proxy.

When separate charge/discharge efficiencies are configured:

$$ \eta_{loss} = (1 - \eta_{chg} \cdot \eta_{dis}) \times 100 $$

Where $\eta_{chg}$ and $\eta_{dis}$ are efficiency fractions (e.g. 0.97).

### Tariff cost

$$ C_{tariff} = \sum_{t \in slots} tariff[t] $$

An optional per-slot fixed tariff cost, typically zero unless the user
configures grid tariff fees.

---

## Score (selector objective)

$$ S = C_{total} + P_{soc} + P_{grid} + P_{override} + V_{terminal} $$

### SoC penalties (quadratic guard)

$$ P_{soc} = \sum_{t \in slots} \begin{cases}
w_{low} \cdot (soc_{min} - soc[t])^2 & \mathrm{if } soc[t] < soc_{min} \\
w_{high} \cdot (soc[t] - soc_{max})^2 & \mathrm{if } soc[t] > soc_{max} \\
0 & \mathrm{otherwise}
\end{cases} $$

These are **soft guards** — the SoC simulation already hard-clamps at hardware
limits, so violations are rare. The quadratic form heavily penalises large
deviations while tolerating tiny numerical rounding errors.

**Past-slot exclusion:** Slots with `time_passed` recommendation are excluded
from SoC penalty calculation. The SoC simulator writes `estimated_battery_soc = 0.0`
as a sentinel on past slots, which would otherwise generate a false penalty of
$w_{low} \cdot soc_{min}^2$ per past slot — identical across all candidates
but log-misleading.

### Grid limit penalty

$$ P_{grid} = \sum_{t \in slots} \max(0, \frac{|gi[t] - ge[t]|}{\Delta t} - L_{grid}) \cdot \Delta t \cdot w_{grid} $$

Where $\Delta t$ is slot duration in hours, $L_{grid}$ is the configured grid
power limit in kW, and $w_{grid}$ is the penalty weight per excess kWh.

### Override penalty

$$ P_{override} = N_{override} \cdot w_{override} $$

Where $N_{override}$ counts slots whose recommendation was forced by an override,
and $w_{override}$ is the cost per override slot.

### Terminal SoC value (opportunity cost)

$$ V_{terminal} = (E_{initial} - E_{final}) \cdot p_{replacement} $$

Where:

- $E_{initial}$ = stored battery energy above the discharge floor at the start of the horizon (kWh)
- $E_{final}$ = stored battery energy above the discharge floor at the end of the horizon (kWh)
- $p_{replacement}$ = replacement price per kWh (minimum future import price)

**Sign convention:**

$$\begin{aligned}
\Delta E &< 0 \mathrm{ (more energy at end)} \rightarrow V_{terminal} < 0 \mathrm{ (credit)} \\
\Delta E &> 0 \mathrm{ (less energy at end)} \rightarrow V_{terminal} > 0 \mathrm{ (penalty)}
\end{aligned}$$

The replacement price uses the **minimum** future import price across the horizon
because:
- It represents the marginal cost of re-purchasing one stored kWh at the cheapest
  opportunity
- Using the average (including expensive peak prices) over-values stored energy
  during high-price periods and biases against discharging

---

## Past-slot exclusion rules

The cost function **skips** any slot whose recommendation is `time_passed`:

- All energy-flow fields (`grid_import_kwh`, `batteries_charged`, etc.) are zero
  on past slots
- Including them would only affect the SoC penalty (bogus $w_{low} \cdot soc_{min}^2$)
- Skipping past slots does not change the winner (the bogus penalty is identical
  across candidates) but keeps the reported cost clean

---

## Cost invariants (test assertions)

For every planner run:

1. $C_{total} = C_{import} - R_{export} + C_{cycle} + C_{loss}$ (exact)
2. No synthetic penalty enters $C_{total}$
3. $S = C_{total} + P_{soc} + P_{grid} + P_{override} + V_{terminal}$ (exact)
4. When all penalties = 0 and terminal-SoC is disabled: $S = C_{total}$
5. Selector picks minimum $S$, not minimum $C_{total}$
6. $score_{winner} = score_{final\_output}$ (no post-selection mutation)
7. Two identical plans, one ending with more stored energy → lower $V_{terminal}$ → lower $S$
