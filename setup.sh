#!/bin/bash
# PolyWhale ops script — install/manage the 24/7 launchd service.
# The plist lives in launchd/com.polywhale.agent.plist (committed).

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/launchd/com.polywhale.agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.polywhale.agent.plist"

case "${1:-help}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load -w "$PLIST_DST"
    echo "installed + started. check: launchctl list | grep polywhale"
    ;;
  uninstall)
    launchctl unload "$PLIST_DST" 2>/dev/null
    rm -f "$PLIST_DST"
    echo "service removed"
    ;;
  restart)
    launchctl unload "$PLIST_DST" && sleep 2 && launchctl load -w "$PLIST_DST"
    echo "restarted"
    ;;
  status)
    launchctl list | grep polywhale || echo "not loaded"
    ps aux | grep "src/agent.py" | grep -v grep
    ;;
  logs)
    tail -f "$(cd "$(dirname "$0")" && pwd)/polywhale.log"
    ;;
  *)
    echo "usage: ./setup.sh {install|uninstall|restart|status|logs}"
    ;;
esac
