# Grid Lens Integration

Compare electricity plans with battery optimization for Home Assistant.

## Installation & Setup

See full documentation in the releases.

## Quick Start

1. Add integration via **Settings** → **Devices & Services**
2. Add cards as resources in **Settings** → **Dashboards** → **Resources**:
   - `/hacsfiles/grid_lens/electricity-plan-comparison-card.js`
   - `/hacsfiles/grid_lens/electricity-energy-flow-card.js`
3. Add cards to your dashboard

## Cards Available

### Plan Comparison Card
```yaml
type: custom:electricity-plan-comparison-card
entity: sensor.grid_lens_amber_monthly_cost
show_breakdown: true
show_battery_schedule: true
```

### Energy Flow Card
```yaml
type: custom:electricity-energy-flow-card
title: "Energy In & Out"
```

Full documentation in releases.
