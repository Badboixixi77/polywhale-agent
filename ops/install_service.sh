#!/bin/bash
# Install PolyWhale as macOS launchd services: the trading agent plus its
# independent watchdog (dead-man's switch). Both survive crashes/reboots.
# Usage: bash ops/install_service.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"

if [ ! -x "$REPO_DIR/venv/bin/python" ]; then
    echo "ERROR: venv/bin/python not found — create the venv first." >&2
    exit 1
fi

for NAME in com.polywhale.agent com.polywhale.watchdog; do
    SRC="$REPO_DIR/ops/$NAME.plist"
    DST="$HOME/Library/LaunchAgents/$NAME.plist"
    launchctl bootout "gui/$UID_NUM/$NAME" 2>/dev/null || true
    cp "$SRC" "$DST"
    launchctl bootstrap "gui/$UID_NUM" "$DST"
    echo "Installed and started: $NAME"
done

echo ""
echo "  status:  launchctl print gui/$UID_NUM/com.polywhale.agent | head -20"
echo "  stop:    launchctl bootout gui/$UID_NUM/com.polywhale.agent"
echo "  logs:    tail -f $REPO_DIR/polywhale.log"
