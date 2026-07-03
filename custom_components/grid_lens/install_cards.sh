#!/bin/bash
# Installation script for Grid Lens cards

echo "Installing Grid Lens custom cards..."

# Check if HA config directory is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <path_to_ha_config>"
    echo "Example: $0 /config"
    exit 1
fi

CONFIG_DIR="$1"
WWW_DIR="$CONFIG_DIR/www"
CARDS_DIR="$WWW_DIR/grid_lens"

# Create www directory if it doesn't exist
if [ ! -d "$WWW_DIR" ]; then
    echo "Creating www directory..."
    mkdir -p "$WWW_DIR"
fi

# Create cards directory
echo "Creating cards directory..."
mkdir -p "$CARDS_DIR"

# Copy card files
echo "Copying card files..."
cp custom_components/grid_lens/www/cards/*.js "$CARDS_DIR/"

# Verify
if [ -f "$CARDS_DIR/electricity-plan-comparison-card.js" ]; then
    echo "✓ electricity-plan-comparison-card.js installed"
else
    echo "✗ Failed to install electricity-plan-comparison-card.js"
    exit 1
fi

if [ -f "$CARDS_DIR/electricity-energy-flow-card.js" ]; then
    echo "✓ electricity-energy-flow-card.js installed"
else
    echo "✗ Failed to install electricity-energy-flow-card.js"
    exit 1
fi

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "1. Add resources in Settings → Dashboards → Resources:"
echo "   - URL: /local/grid_lens/electricity-plan-comparison-card.js"
echo "   - Type: JavaScript Module"
echo ""
echo "   - URL: /local/grid_lens/electricity-energy-flow-card.js"
echo "   - Type: JavaScript Module"
echo ""
echo "2. Hard refresh browser (Ctrl+Shift+R)"
echo "3. Add cards to dashboard"
