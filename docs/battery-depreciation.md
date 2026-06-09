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

```
Depreciation per kWh = (Purchase Price × Capacity Loss) / (Number of Cycles × Battery Capacity)
```

Where:

| Parameter | Description |
|---|---|
| **Purchase Price** | Total battery system cost (in your currency) |
| **Capacity Loss** | Maximum capacity reduction over lifetime (percentage, as decimal) |
| **Number of Cycles** | Total expected complete charge/discharge cycles |
| **Battery Capacity** | Total usable energy storage (in kWh) |

---

## Example Calculation

Parameters for a LiFePO4 battery system:

- **Purchase Price**: 48,000 DKK
- **Capacity Loss**: 30% (0.30)
- **Number of Cycles**: 6,000 full cycles
- **Battery Capacity**: 10 kWh

```
Depreciation per kWh = (48,000 × 0.30) / (6,000 × 10)
                     = 14,400 / 60,000
                     = 0.24 DKK/kWh
```

Each kWh stored and discharged through the battery costs **0.24 DKK** in wear and tear.

---

## Practical Implication

To make charging economically viable, the price difference between charging and discharging must exceed the depreciation cost. For example:

- If charging electricity costs 1.00 DKK/kWh, the discharge price must be at least **1.24 DKK/kWh** to offset depreciation.

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

HSEM uses this formula in the **Excess Battery Export** feature and battery schedule economics. The recommended threshold is calculated automatically during configuration using the battery purchase price, expected cycles, and usable capacity you provide.

**Formula used by HSEM:**
```
Depreciation = (Purchase_Price × Capacity_Loss_Pct / 100) / (2 × Expected_Cycles × Usable_Capacity)
```

Note the **2× denominator** — this accounts for both charge and discharge halves of a full cycle, and is the canonical formula used throughout HSEM's planner and cost function.

---

## References

- [Smart Home Guide: Opladning af husbatteri](https://smart-home-guide.dk/index.php/2024/11/10/opladning-af-husbatteri/)
