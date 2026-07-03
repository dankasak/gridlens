#!/bin/sh
# Sync the integration from the repo to the live HA location, then restart HA.
# Run this after editing files in custom_components/grid_lens/.

REPO="$(dirname "$0")/custom_components/grid_lens"
LIVE="/homeassistant/custom_components/grid_lens"

cp -r "$REPO/." "$LIVE/"
echo "Synced to $LIVE"

echo "Restarting HA..."
ha core restart
