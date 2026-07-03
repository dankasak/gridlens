# Grid Lens

Real-time electricity plan comparison and battery optimisation for Australian households.

## Prerequisites

- **MariaDB** app must be installed and running (Grid Lens uses it for storage)

## Configuration

| Option | Required | Description |
|--------|----------|-------------|
| `admin_token` | Yes | Secret token for admin API endpoints |
| `stripe_secret_key` | No | Stripe secret key (for subscription billing) |
| `stripe_webhook_secret` | No | Stripe webhook secret |
| `stripe_price_id` | No | Stripe price ID for the subscription tier |
| `timescaledb_url` | No | External TimescaleDB connection URL for historical price data |
| `log_level` | No | Logging verbosity (default: `info`) |

## API

Once running, the API is available at `http://homeassistant.local:8000`.

The Grid Lens integration for Home Assistant connects to this API automatically
if installed on the same machine. For remote installs, configure the API URL
in the integration settings.

## Grid Lens Integration

Install the **Grid Lens** integration via HACS to add plan-comparison sensors
to your Home Assistant dashboards.
