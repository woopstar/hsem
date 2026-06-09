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
    badges: []
    sections:
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
    type: sections
    max_columns: 2
    cards: []
```

---

## Dashboard layout

The dashboard uses a three-section layout within one view:

| Section | Column span | Cards | Description |
|---|---|---|---|
| **Main** | 2 columns | 7 | Recommendation timeline, battery status, charged kWh, consumption breakdown |
| **Middle** | 1 column | 4 | Net consumption, estimated cost, consumption, export price |
| **Right** | 1 column | 4 | Battery capacity, simulated SoC, PV forecast, import price |

---

## Cards reference

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
