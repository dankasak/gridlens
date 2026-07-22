# Changelog

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
