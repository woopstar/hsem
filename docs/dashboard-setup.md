# HSEM Dashboard Setup

Step-by-step guide for setting up the HSEM ApexCharts dashboard in Home Assistant.

---

## Prerequisites

- **[ApexCharts Card](https://github.com/RomRider/apexcharts-card)** installed via HACS
- **HSEM integration** configured and running (the `sensor.hsem_workingmode_sensor` entity must be available)
- The `hourly_recommendations` attribute must be populated (wait for the first planner cycle to complete)

---

## Setup instructions

1. In Home Assistant, go to **Settings** → **Dashboards**.
2. Click the three-dot menu in the top-right corner and select **Raw configuration editor**.
3. Paste the YAML below into your dashboard.
4. Replace `sensor.batteries_state_of_capacity` and `sensor.power_import` with your own battery SoC and grid import power entities.
5. Save and refresh your dashboard.

> **Tip:** Start in **Read-Only** mode (`switch.hsem_read_only` on) to safely review planner recommendations before enabling hardware writes.

---

## Full dashboard YAML

```yaml
views:
  - title: HSEM
    type: sections
    max_columns: 2
    cards: []
    badges: []
    sections:
      - type: grid
        cards:
          - type: heading
            heading: HSEM Status & Control
            heading_style: title
            grid_options:
              columns: full
              rows: 2
          - type: tile
            entity: sensor.hsem_workingmode_sensor
            name: Working Mode
            icon: mdi:robot
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_degraded_mode_sensor
            name: System Health
            icon: mdi:heart-pulse
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: switch.hsem_read_only
            name: Read-Only
            icon: mdi:lock
            color: state
            features:
              - type: toggle
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_plan_explanation_sensor
            name: Plan Strategy
            icon: mdi:strategy
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: select.hsem_force_working_mode
            name: Force Mode
            icon: mdi:hand-back-right
            color: state
            features:
              - type: select-options
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_next_update_sensor
            name: Next Update
            icon: mdi:update
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_applier_status_sensor
            name: Inverter Apply
            icon: mdi:transmission-tower
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_hardware_writes_sensor
            name: Hardware Writes
            icon: mdi:pencil-off
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_pv_curtailment_sensor
            name: PV Curtailment
            icon: mdi:solar-power
            color: state
            grid_options:
              columns: 2
      - type: grid
        cards:
          - type: heading
            heading: Financial Insights
            heading_style: title
            grid_options:
              columns: full
              rows: 2
          - type: tile
            entity: sensor.hsem_export_income
            name: Export Income
            icon: mdi:cash-plus
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_import_cost
            name: Import Cost
            icon: mdi:cash-minus
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_net_grid_balance
            name: Net Grid Balance
            icon: mdi:scale-balance
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_savings_tracker
            name: Savings Tracker
            icon: mdi:piggy-bank
            color: state
            grid_options:
              columns: 2
      - type: grid
        cards:
          - type: heading
            heading: Forecast Quality
            heading_style: title
            grid_options:
              columns: full
              rows: 2
          - type: tile
            entity: sensor.hsem_forecast_accuracy_sensor
            name: Forecast Accuracy
            icon: mdi:chart-line
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_prediction_accuracy_sensor
            name: Prediction Accuracy
            icon: mdi:target
            color: state
            grid_options:
              columns: 2
          - type: tile
            entity: sensor.hsem_solar_confidence_sensor
            name: Solar Confidence
            icon: mdi:solar-power
            color: state
            grid_options:
              columns: 2
      - type: grid
        cards:
          - type: heading
            heading: EV Charging
            heading_style: title
            grid_options:
              columns: full
              rows: 2
          - type: conditional
            conditions:
              - entity: sensor.hsem_ev_optimal_charging_plan
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: sensor.hsem_ev_optimal_charging_plan
              name: EV Plan
              icon: mdi:ev-station
              color: state
          - type: conditional
            conditions:
              - entity: sensor.hsem_ev_charging_sensor
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: sensor.hsem_ev_charging_sensor
              name: EV Charging Active
              icon: mdi:lightning-bolt
              color: state
          - type: conditional
            conditions:
              - entity: switch.hsem_ev_smart_charging
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: switch.hsem_ev_smart_charging
              name: EV Smart Charging
              icon: mdi:brain
              color: state
              features:
                - type: toggle
          - type: conditional
            conditions:
              - entity: switch.hsem_ev_force_charge_now
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: switch.hsem_ev_force_charge_now
              name: EV Force Charge
              icon: mdi:lightning-bolt-outline
              color: state
              features:
                - type: toggle
          - type: conditional
            conditions:
              - entity: number.hsem_ev_target_soc
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: number.hsem_ev_target_soc
              name: EV Target SoC
              icon: mdi:battery-charging
              color: state
          - type: conditional
            conditions:
              - entity: time.hsem_ev_deadline_time
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: time.hsem_ev_deadline_time
              name: EV Deadline
              icon: mdi:clock-end
              color: state
          - type: conditional
            conditions:
              - entity: sensor.hsem_ev_second_optimal_charging_plan
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: sensor.hsem_ev_second_optimal_charging_plan
              name: EV 2 Plan
              icon: mdi:ev-station
              color: state
          - type: conditional
            conditions:
              - entity: switch.hsem_ev_second_smart_charging
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: switch.hsem_ev_second_smart_charging
              name: EV 2 Smart Charging
              icon: mdi:brain
              color: state
              features:
                - type: toggle
          - type: conditional
            conditions:
              - entity: switch.hsem_ev_second_force_charge_now
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: switch.hsem_ev_second_force_charge_now
              name: EV 2 Force Charge
              icon: mdi:lightning-bolt-outline
              color: state
              features:
                - type: toggle
          - type: conditional
            conditions:
              - entity: number.hsem_ev_second_target_soc
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: number.hsem_ev_second_target_soc
              name: EV 2 Target SoC
              icon: mdi:battery-charging
              color: state
          - type: conditional
            conditions:
              - entity: time.hsem_ev_second_deadline_time
                state_not: unavailable
            grid_options:
              columns: 2
            card:
              type: tile
              entity: time.hsem_ev_second_deadline_time
              name: EV 2 Deadline
              icon: mdi:clock-end
              color: state
      - type: grid
        cards:
          - type: heading
            heading: HSEM Working Mode Recommendation
            heading_style: title
            grid_options:
              columns: full
              rows: 2
          - type: custom:apexcharts-card
            update_interval: 5m
            experimental:
              disable_config_validation: true
            grid_options:
              columns: full
            layout_options:
              grid_columns: 3
              grid_rows: 1
            header:
              show: false
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 120
              stroke:
                curve: stepline
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                show: false
                min: 0
                max: 1
                tickAmount: 1
            series:
              - name: Batteries Charge From Grid
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'batteries_charge_grid' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#ef4444"
              - name: Batteries Charge From Solar
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'batteries_charge_solar' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#22c55e"
              - name: Batteries Discharge Mode
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'batteries_discharge_mode' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#f97316"
              - name: Batteries Wait Mode
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'batteries_wait_mode' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#8b5cf6"
              - name: EV Smart Charging
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'ev_smart_charging' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#06b6d4"
              - name: Time Passed
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'time_passed' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#64748b"
              - name: Force Batteries Discharge
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                opacity: 1
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, recommendation })
                  => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    const on = recommendation === 'force_batteries_discharge' ? 1 : null;
                    out.push([s, on], [e, on]);
                  }); return out;
                color: "#ec4899"
          - type: tile
            entity: sensor.hsem_workingmode_sensor
            features_position: bottom
            vertical: false
            grid_options:
              columns: 24
              rows: 1
          - type: heading
            heading: Battery
            heading_style: title
          - type: custom:apexcharts-card
            update_interval: 10m
            apex_config:
              chart:
                height: 150px
              legend:
                show: false
              xaxis:
                labels:
                  show: true
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 10
            header:
              show: true
              show_states: true
              colorize_states: true
            all_series_config:
              type: area
              opacity: 0.3
              stroke_width: 1
            series:
              - entity: sensor.batteries_state_of_capacity
                type: line
                color: "#eab308"
                yaxis_id: pct
                opacity: 1
                stroke_width: 2
              - entity: sensor.power_import
                color: "#ef4444"
                yaxis_id: watt
                group_by:
                  func: avg
                  duration: 5min
            yaxis:
              - id: pct
                show: true
                opposite: false
                decimals: 0
                max: 100
                min: 0
              - id: watt
                show: true
                opposite: true
                decimals: 0
                min: 0
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: batteries_charged_kwh
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: batteries_charged_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#22c55e"
                float_precision: 3
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  batteries_charged_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, batteries_charged_kwh], [e, batteries_charged_kwh]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 1m
            experimental:
              disable_config_validation: true
            grid_options:
              columns: full
            layout_options:
              grid_columns: 3
              grid_rows: 1
            graph_span: 24h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 500
              stroke:
                width: 2
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
            series:
              - name: avg_house_consumption_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: area
                color: "#f97316"
                float_precision: 3
                opacity: 0.3
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_kwh], [e, avg_house_consumption_kwh]);
                  }); return out;
              - name: avg_house_consumption_1d_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#3b82f6"
                float_precision: 3
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_1d_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_1d_kwh], [e, avg_house_consumption_1d_kwh]);
                  }); return out;
              - name: avg_house_consumption_3d
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#eab308"
                float_precision: 3
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_3d_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_3d_kwh], [e, avg_house_consumption_3d_kwh]);
                  }); return out;
              - name: avg_house_consumption_7d_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#8b5cf6"
                float_precision: 3
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_7d_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_7d_kwh], [e, avg_house_consumption_7d_kwh]);
                  }); return out;
              - name: avg_house_consumption_14d_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#22c55e"
                float_precision: 3
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_14d_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_14d_kwh], [e, avg_house_consumption_14d_kwh]);
                  }); return out;
        column_span: 2
      - type: grid
        cards:
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: estimated_net_consumption_kwh
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: estimated_net_consumption_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#06b6d4"
                float_precision: 3
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  estimated_net_consumption_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, estimated_net_consumption_kwh], [e, estimated_net_consumption_kwh]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: estimated_cost
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 2
                opposite: true
            series:
              - name: estimated_cost_currency
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#22c55e"
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  estimated_cost_currency }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, estimated_cost_currency], [e, estimated_cost_currency]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: avg_house_consumption_kwh
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: Estimated consumption
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                float_precision: 3
                color: "#ef4444"
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  avg_house_consumption_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, avg_house_consumption_kwh], [e, avg_house_consumption_kwh]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: export_price
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: export_price
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#22c55e"
                float_precision: 3
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, export_price }) =>
                  {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, export_price], [e, export_price]);
                  }); return out;
      - type: grid
        cards:
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: estimated_battery_capacity_kwh
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 0
                opposite: true
            series:
              - name: estimated_battery_capacity_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#06b6d4"
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  estimated_battery_capacity_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, estimated_battery_capacity_kwh], [e, estimated_battery_capacity_kwh]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: estimated_battery_soc_pct
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 0
                opposite: true
            series:
              - name: estimated_battery_soc_pct
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#eab308"
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  estimated_battery_soc_pct }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, estimated_battery_soc_pct], [e, estimated_battery_soc_pct]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: solcast_pv_estimate_kwh
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: solcast_pv_estimate_kwh
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#f59e0b"
                float_precision: 3
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end,
                  solcast_pv_estimate_kwh }) => {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, solcast_pv_estimate_kwh], [e, solcast_pv_estimate_kwh]);
                  }); return out;
          - type: custom:apexcharts-card
            update_interval: 5m
            header:
              show: true
              title: import_price
            graph_span: 48h
            span:
              start: day
            now:
              show: true
              color: red
              label: Now
            apex_config:
              chart:
                height: 180
              stroke:
                curve: stepline
                width: 1
              markers:
                size: 0
              xaxis:
                labels:
                  format: HH
                  rotate: -45
                  rotateAlways: true
                  hideOverlappingLabels: true
                  style:
                    fontSize: 10
                    fontWeight: 500
              yaxis:
                decimalsInFloat: 3
                opposite: true
            series:
              - name: import_price
                entity: sensor.hsem_workingmode_sensor
                attribute: hourly_recommendations
                type: line
                color: "#ef4444"
                float_precision: 3
                show:
                  legend_value: false
                data_generator: >
                  const rows =
                  (Array.isArray(entity.attributes.hourly_recommendations) ?
                  entity.attributes.hourly_recommendations : [])
                    .slice()
                    .sort((a,b) => new Date(a.start) - new Date(b.start));
                  const out = []; rows.forEach(({ start, end, import_price }) =>
                  {
                    const s = new Date(start).getTime();
                    const e = new Date(end).getTime();
                    out.push([s, import_price], [e, import_price]);
                  }); return out;

```

---

## Dashboard layout

The dashboard uses a seven-section layout within one view:

| Section | Column span | Cards | Description |
|---|---|---|---|
| **Status & Control** | 1 column | 10 | Working mode, system health, read-only toggle, plan strategy, force mode, next update, inverter apply, hardware writes, PV curtailment |
| **Financial Insights** | 1 column | 5 | Export income, import cost, net grid balance, savings tracker |
| **Forecast Quality** | 1 column | 4 | Forecast accuracy, prediction accuracy, solar confidence |
| **EV Charging** | 1 column | 12 | EV plan, active status, smart charging, force charge, target SoC, deadline (primary + secondary, conditional) |
| **Working Mode Recommendation** | 2 columns | 7 | Recommendation timeline, battery status, charged kWh, consumption breakdown |
| **Planner Output (left)** | 1 column | 4 | Net consumption, estimated cost, consumption, export price |
| **Planner Output (right)** | 1 column | 4 | Battery capacity, simulated SoC, PV forecast, import price |

---

## Cards reference

### Status & Control tiles

The top section uses built-in `tile` cards to surface the most important HSEM
state at a glance and to provide quick controls:

| Card | Entity | Purpose |
|---|---|---|
| Working Mode | `sensor.hsem_workingmode_sensor` | Current planner recommendation |
| System Health | `sensor.hsem_degraded_mode_sensor` | `ok` / `degraded` / `error` state |
| Read-Only | `switch.hsem_read_only` | Toggle hardware write protection |
| Plan Strategy | `sensor.hsem_plan_explanation_sensor` | Why the planner chose the current plan |
| Force Mode | `select.hsem_force_working_mode` | Override the planner working mode |
| Next Update | `sensor.hsem_next_update_sensor` | Countdown to the next planner cycle |
| Inverter Apply | `sensor.hsem_applier_status_sensor` | Last hardware write result |
| Hardware Writes | `sensor.hsem_hardware_writes_sensor` | Whether writes are currently blocked |
| PV Curtailment | `sensor.hsem_pv_curtailment_sensor` | `curtailed` when PV is being throttled |

### Financial Insights tiles

Quick visibility into the financial impact of the planner:

| Card | Entity | Purpose |
|---|---|---|
| Export Income | `sensor.hsem_export_income` | Cumulative revenue from grid exports |
| Import Cost | `sensor.hsem_import_cost` | Cumulative cost of grid imports |
| Net Grid Balance | `sensor.hsem_net_grid_balance` | Export income minus import cost |
| Savings Tracker | `sensor.hsem_savings_tracker` | Actual savings vs missed savings |

### Forecast Quality tiles

Visibility into how well the planner's forecasts match reality:

| Card | Entity | Purpose |
|---|---|---|
| Forecast Accuracy | `sensor.hsem_forecast_accuracy_sensor` | PV and load forecast MAE |
| Prediction Accuracy | `sensor.hsem_prediction_accuracy_sensor` | SoC prediction MAE over 7/30 days |
| Solar Confidence | `sensor.hsem_solar_confidence_sensor` | Learned per-hour solar correction factors |

### EV Charging tiles

All EV cards are wrapped in `conditional` cards so they only appear when the
corresponding EV entity is available. The section covers the primary EV and a
secondary EV when configured:

| Card | Entity | Purpose |
|---|---|---|
| EV Plan | `sensor.hsem_ev_optimal_charging_plan` | Current EV charging plan state |
| EV Charging Active | `sensor.hsem_ev_charging_sensor` | Whether an EV is currently charging |
| EV Smart Charging | `switch.hsem_ev_smart_charging` | Enable/disable smart EV charging |
| EV Force Charge | `switch.hsem_ev_force_charge_now` | Override and start charging now |
| EV Target SoC | `number.hsem_ev_target_soc` | Target state of charge for smart charging |
| EV Deadline | `time.hsem_ev_deadline_time` | Deadline by which the EV must reach target SoC |
| EV 2 Plan | `sensor.hsem_ev_second_optimal_charging_plan` | Second EV charging plan state |
| EV 2 Smart Charging | `switch.hsem_ev_second_smart_charging` | Enable/disable smart charging for second EV |
| EV 2 Force Charge | `switch.hsem_ev_second_force_charge_now` | Override and start second EV charging now |
| EV 2 Target SoC | `number.hsem_ev_second_target_soc` | Target SoC for second EV |
| EV 2 Deadline | `time.hsem_ev_second_deadline_time` | Deadline for second EV |

### Recommendation timeline chart

The top stepline area chart renders 48 hours of planner recommendations. Each
recommendation type is a color-coded horizontal band.

| Series | Color | Recommendation string |
|---|---|---|
| Batteries Charge From Grid | `#ef4444` (red) | `batteries_charge_grid` |
| Batteries Charge From Solar | `#22c55e` (green) | `batteries_charge_solar` |
| Batteries Discharge Mode | `#f97316` (orange) | `batteries_discharge_mode` |
| Batteries Wait Mode | `#8b5cf6` (purple) | `batteries_wait_mode` |
| EV Smart Charging | `#06b6d4` (cyan) | `ev_smart_charging` |
| Time Passed | `#64748b` (slate) | `time_passed` |
| Force Batteries Discharge | `#ec4899` (pink) | `force_batteries_discharge` |

### Battery status chart

Dual-axis chart combining live battery SoC (`sensor.batteries_state_of_capacity`)
with 5-minute averaged grid import power (`sensor.power_import`).

> **Customize:** Replace `sensor.batteries_state_of_capacity` and `sensor.power_import`
> with your own entity IDs. These come from your inverter/sensor integrations.

### Planner output charts

The remaining 11 charts each plot a single field from `hourly_recommendations`
as a stepline across the 48-hour horizon:

| Chart title | Field | Unit |
|---|---|---|
| `batteries_charged_kwh` | `batteries_charged_kwh` | kWh |
| Consumption breakdown (5 series) | `avg_house_consumption_kwh`, `..._1d/3d/7d/14d_kwh` | kWh |
| `estimated_net_consumption_kwh` | `estimated_net_consumption_kwh` | kWh |
| `estimated_cost` | `estimated_cost_currency` | Currency |
| `avg_house_consumption_kwh` | `avg_house_consumption_kwh` | kWh |
| `export_price` | `export_price` | Currency/kWh |
| `estimated_battery_capacity_kwh` | `estimated_battery_capacity_kwh` | kWh |
| `estimated_battery_soc_pct` | `estimated_battery_soc_pct` | % |
| `solcast_pv_estimate_kwh` | `solcast_pv_estimate_kwh` | kWh |
| `import_price` | `import_price` | Currency/kWh |

---

## `data_generator` pattern

All HSEM charts use the same JavaScript pattern to extract data from
`hourly_recommendations`:

1. Guard against missing/non-array data with `Array.isArray()`.
2. Sort slots chronologically by `start` timestamp.
3. Map each slot to `[timestamp_ms, value]` pairs for ApexCharts.
4. For stepline charts, push both `[start, value]` and `[end, value]` to
   create horizontal segments with sharp vertical transitions.

Example for a single-field stepline:

```javascript
const rows = (Array.isArray(entity.attributes.hourly_recommendations)
  ? entity.attributes.hourly_recommendations : [])
    .slice()
    .sort((a, b) => new Date(a.start) - new Date(b.start));
const out = [];
rows.forEach(({ start, end, field_name }) => {
  const s = new Date(start).getTime();
  const e = new Date(end).getTime();
  out.push([s, field_name], [e, field_name]);
});
return out;
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Charts show no data | Verify `sensor.hsem_workingmode_sensor` exists and `hourly_recommendations` attribute is populated |
| All bands are gray (`time_passed`) | The planner hasn't run a new cycle yet — wait for the next update interval |
| Charts are offset by one hour | Check your Home Assistant timezone matches your local time |
| Battery SoC chart is empty | Replace `sensor.batteries_state_of_capacity` with your actual battery SoC entity |
| Grid import chart is empty | Replace `sensor.power_import` with your actual grid import power entity |
