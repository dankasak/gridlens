# Grid Lens — Home Assistant Integration

Electricity plan comparison, battery optimisation, and deferrable-load control for
Australian households. Everything runs locally in your Home Assistant instance; your usage
data never leaves your network.

## Install

Via HACS as a custom repository (`https://github.com/dankasak/gridlens`, category
**Integration**), then restart Home Assistant and add the integration under
**Settings → Devices & Services → Add Integration → Grid Lens**.

Full setup guide: <https://gridlens.au/docs.html>

## Cards

**You don't need to configure any of these.** Grid Lens registers its own Lovelace resources
and seeds a **Grid Lens** dashboard into your sidebar on first setup. The cards below are
listed for anyone building their own dashboard — each one auto-discovers its entities, so a
bare `type:` is usually all you need.

| Card | Shows |
|---|---|
| `custom:grid-lens-card` | Full plan comparison |
| `custom:grid-lens-powerflow-card` | Live radial energy flow (solar / grid / battery / home / loads) |
| `custom:grid-lens-power-chart-card` | Measured & forecast power, with free-energy shading |
| `custom:grid-lens-price-chart-card` | Import/export rate trajectory |
| `custom:grid-lens-soc-chart-card` | Battery state-of-charge curve |
| `custom:grid-lens-cash-chart-card` | Cumulative cost and credit |
| `custom:grid-lens-dispatch-chart-card` | Planned battery mode timeline |
| `custom:grid-lens-advisory-card` | Plan status tiles |
| `custom:grid-lens-load-control-card` | Per-appliance control, boost, and greedy settings |
| `custom:grid-lens-defer-schedule-card` | Weekly allowed-run-times editor |

## Documentation

- **Users:** <https://gridlens.au/docs.html>
- **Developers / contributors:** [`FEATURES.md`](../../FEATURES.md) in the repo root — the
  current-state map of every feature, the entities it creates, and where it's implemented.

## Tests

Offline suites, no Home Assistant or scipy required:

```bash
python3 tests/test_<name>.py
```

## Licence

See [`LICENSE`](../../LICENSE).
