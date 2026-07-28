# Changelog

## [3.1.0] - 2026-07-29

### Added
- Power Flow card: live buy/sell price line under Grid's status, read from a GridLens
  dispatch sensor's planned rate trajectory (auto-discovered, or pin one via
  `price_source_entity`).
- Power Flow card: connectors now render as a rail plus a stream of pulsating balls
  (SMIL animation) sized against a shared hardware ceiling (`max_ball_kw`, auto-read from
  the dispatch sensor's battery charge/discharge limits) so flows are visually comparable
  across nodes instead of each scaled to its own node's capacity.
- Power Flow card: `solar_power_entity` auto-discovers from Home Assistant's own Energy
  Dashboard preferences when left unset — the one universal live-power slot HA core
  provides.
- Power chart card: Buy/Sell collapsed into one signed net Grid line, plus a new signed
  Battery (charge/discharge) line; y-axis is forced symmetric around 0 for these.
- `entity_lookup`: deferrable device display names now prefer the Energy Dashboard's own
  per-device label (where a rename like "Hot Water" actually lives) ahead of the entity
  registry's original name.
- New-install dashboard seeding (`__init__.py`) now builds the full Plan Comparison +
  Battery Plan view by resolving every entity from the registry via each platform's
  unique_id, instead of a single hardcoded card.
- `AdvisoryResult` exposes `deferrable_sensor_ids` (a reliable join key between a
  trajectory device slot and its real power sensor) and this install's configured
  `battery_max_charge_kw` / `battery_max_discharge_kw`.

### Changed
- Power Flow card: no more installed-brand default entities (previously hardcoded to this
  dev rig's Sigenergy/EVConduit ids) — every entity except auto-discovered solar must now
  be configured explicitly.
- Power Flow card: dedicated colour for hot water instead of cycling through the generic
  deferrable-load palette; smaller Home hub sized to match peripheral nodes; palette
  shared with the chart cards so the same flow reads as the same colour everywhere.
- `sigenergy_mqtt` default entity ids updated to the current `sigenergy2mqtt` add-on
  naming (old ids carried a stray `_2` / `_plant_ess_` suffix left over from a
  now-uninstalled legacy Modbus integration).

### Fixed
- Power chart card: chart no longer letterboxes empty space above/below the plot when a
  card pins an explicit height whose aspect ratio doesn't match the SVG viewBox
  (`preserveAspectRatio="none"`); header/legend spacing tightened so more of the card's
  height goes to the chart itself instead of its header chrome.
- Options flow no longer fails `SelectSelector` validation for the whole deferrable-loads
  field when a previously-saved sensor selection has since been renamed or removed.

## [3.0.3] - 2026-07-22

### Added
- Conditional day-credits: the LP optimizer now models day-scoped all-or-nothing
  bonuses like GloBird ZEROHERO's "$1/day when imports are 0.03 kWh/hour or less,
  6pm-9pm" via a MILP binary indicator (switches the scipy solve from `linprog` to
  `scipy.optimize.milp` only for plans that carry one). New `scipy>=1.9.0`
  requirement (was already an undeclared transitive dependency).
- Plan-comparison ranking now accounts for the earned credit, and the bill
  breakdown shows a per-credit line (days earned vs. days in the schedule).

### Fixed
- `scipy` was imported but never declared in `manifest.json`'s requirements.

(Changelog gap 2026-02-24 → 2026-07-22: several releases shipped without an entry
here — manifest.json is the authoritative version history for that period.)

## [0.2.3] - 2026-02-24

### Fixed
- Static path registration now uses synchronous `register_static_path` method
- Works correctly with current Home Assistant versions

## [0.2.2] - 2026-02-24

### Fixed
- Attempted to use `async_register_static_paths` (didn't work correctly)

## [0.2.1] - 2026-02-24

### Changed
- **Dashboard now auto-registers in sidebar** - no configuration.yaml editing required!
- Panel appears automatically after integration installation
- Accessible at `/electricity-plans` in sidebar
- Uses Home Assistant's built-in panel registration

### Removed
- Manual panel_iframe configuration requirement

## [0.2.0] - 2026-02-24

### Added
- **Integrated interactive dashboard** accessible at `/electricity_plan_dashboard/`
- Dashboard automatically reads sensor configuration from integration
- No code editing required - sensors configured once in setup wizard
- Panel iframe support for sidebar integration
- Date range selector (7/30/90/365 days)
- Visual charts for daily energy flow and cost comparison
- Export credits fully visualized
- Sensor configuration exposed in attributes for dashboard access

### Changed
- Sensor configuration method: wizard → integration → dashboard (automatic)
- Dashboard location: now served by integration at `/electricity_plan_dashboard/`
- Documentation simplified (removed old guides)

### Removed
- Standalone dashboard files (now integrated)
- Redundant documentation files
- Manual sensor ID configuration requirement

## [0.1.5] - 2026-02-14

### Added
- Grid export tracking and feed-in credits
- Export sensor configuration
- Feed-in price sensor configuration
- Net cost calculation (import - export)

## [0.1.4] - 2026-02-14

### Added
- Solar self-consumption support
- Grid import calculation (load - solar)
- Improved wizard descriptions

## [0.1.3] - 2026-02-14

### Fixed
- Cumulative sensor handling (delta calculation)
- MWh to kWh conversion
- Cost calculation accuracy

## [0.1.2] - 2026-02-14

### Fixed
- Config flow data preservation across wizard steps

## [0.1.1] - 2026-02-14

### Fixed
- Setup completion with no historical data
- Graceful "waiting for data" state

## [0.1.0] - 2026-02-14

### Initial Release
- Three sensors for cost comparison
- Config flow wizard
- Basic cost calculations
- Support for NSW, VIC, QLD, SA, WA, TAS, NT, ACT
