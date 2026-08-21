#!/bin/sh
# Sync the integration from the repo to the live HA location, then restart HA.
# Run this after editing files in custom_components/grid_lens/.

REPO="$(dirname "$0")/custom_components/grid_lens"
LIVE="/homeassistant/custom_components/grid_lens"

# HA loads translations/en.json for a custom component, never strings.json — so any
# label only added to strings.json renders as its raw key ("gridlens_email") in the
# UI. They drifted apart once already and put raw variable names on the first screen
# of setup; regenerating here makes that impossible rather than merely discouraged.
cp "$REPO/strings.json" "$REPO/translations/en.json"

cp -r "$REPO/." "$LIVE/"
echo "Synced to $LIVE"

echo "Restarting HA..."
ha core restart
