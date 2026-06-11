# How to Calculate the Minimum Charging Price for a Battery Schedule

When using a battery system, it's crucial to consider the cost of wear and tear associated with charging and discharging the battery. This cost is referred to as **depreciation per kWh**, and understanding it allows you to determine the minimum price difference (charging vs. discharging) that makes it economically viable to charge the battery.

---

## Key Concepts

### Depreciation per kWh

The battery wear cost for each kWh of energy charged and discharged. It accounts for the battery's purchase price, capacity loss over its lifetime, the number of charge/discharge cycles it can handle, and the total energy throughput.

### Battery Cycle

For LiFePO4 batteries, a **cycle** is one complete charge and discharge:

- A **full cycle**: charged from 0% to 100% and discharged back to 0%.
- **Partial cycles** also count. E.g., charging from 50% to 100% and back to 50% = half a cycle. Two partial 50% cycles = one full cycle.

LiFePO4 batteries are designed for 6,000–8,000 full cycles before capacity degrades significantly (typically 20–30% reduction).

### Depth of Discharge (DoD)

Measures how much of the battery's total capacity is used during a cycle. A 70% DoD means only 70% of the battery's energy is used, which increases overall lifespan compared to 100% DoD.

### Simplified Model

This model calculates depreciation per kWh assuming:

- Full cycles (100% DoD).
- Uniform capacity loss (degeneration) over the battery's lifetime.
- No operational inefficiencies (e.g., energy conversion losses).

---

## Formula

Let:

| Symbol | Parameter | Description |
|---|---|---|
| $P$ | Purchase price | Total battery system cost (in your currency) |
| $L$ | Capacity loss | Maximum capacity reduction over lifetime as a fraction, e.g. $0.30$ for 30% |
| $N$ | Number of cycles | Total expected complete charge/discharge cycles |
| $C$ | Battery capacity | Total usable energy storage in kWh |
| $D$ | Depreciation per kWh | Battery wear cost per kWh |

The simplified depreciation per kWh is:

$$
D = \frac{P \times L}{N \times C}
$$

---

## Example Calculation

Parameters for a LiFePO4 battery system:

- **Purchase Price**: 48,000 DKK
- **Capacity Loss**: 30% ($L = 0.30$)
- **Number of Cycles**: 6,000 full cycles
- **Battery Capacity**: 10 kWh

$$
\begin{aligned}
D &= \frac{48{,}000 \times 0.30}{6{,}000 \times 10} \\
  &= \frac{14{,}400}{60{,}000} \\
  &= 0.24\ \text{DKK/kWh}
\end{aligned}
$$

Each kWh stored and discharged through the battery costs **0.24 DKK** in wear and tear.

---

## Practical Implication

To make charging economically viable, the price difference between charging and discharging must exceed the depreciation cost:

$$
\text{Discharge price} - \text{Charge price} \ge D
$$

For example, if charging electricity costs 1.00 DKK/kWh:

$$
\text{Minimum discharge price} = 1.00 + 0.24 = 1.24\ \text{DKK/kWh}
$$

---

## Limitations of the Simplified Model

- **Partial Cycles & DoD**: The calculation assumes 100% DoD. Real-world usage involves partial cycles that extend battery life.
- **Efficiency Losses**: Charging/discharging involve 5–15% energy loss in the battery and inverter not accounted for here.
- **Operating Conditions**: Temperature, charging speed, and discharging rate impact battery lifespan.
- **Time-Based Degradation**: Batteries degrade over time even when not cycled.
- **Maintenance Costs**: Does not include potential maintenance or replacement costs for inverters or BMS.

> For a more accurate assessment, include DoD, conversion losses, and real-world operating conditions in a comprehensive analysis.

---

## HSEM Integration

HSEM uses a round-trip cycle-cost formula in the **Excess Battery Export** feature and battery schedule economics. The recommended threshold is calculated automatically during configuration using the battery purchase price, expected cycles, capacity loss, usable capacity, efficiency, and grid-fee inputs you provide.

Let:

| Symbol | Parameter | Description |
|---|---|---|
| $P$ | Purchase price | Battery system purchase price |
| $L_{pct}$ | Capacity loss percent | Lifetime capacity loss percentage |
| $N$ | Expected cycles | Expected full-cycle lifetime |
| $C_u$ | Usable capacity | Usable battery capacity in kWh |
| $\alpha$ | Cycle cost per kWh | HSEM battery wear cost per kWh |

The canonical HSEM cycle-cost formula is:

$$
\alpha = \frac{P \times \left(\frac{L_{pct}}{100}\right)}{2 \times N \times C_u}
$$

The **$2 \times$ denominator is mandatory**. It accounts for one full round-trip: a complete charge and discharge moves $2 \times C_u$ kWh of throughput per cycle.

---

## References

- [Smart Home Guide: Opladning af husbatteri](https://smart-home-guide.dk/index.php/2024/11/10/opladning-af-husbatteri/)
