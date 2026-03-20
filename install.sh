#!/bin/bash
# imessage-bridge installer
# Installs the bridge server and sets up a launchd service.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/greghughespdx/imessage-bridge/main/install.sh | bash
#   -- or --
#   bash install.sh

set -euo pipefail

INSTALL_DIR="$HOME/.imessage-bridge"
PLIST_NAME="com.imessage-bridge.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
PORT="${IMESSAGE_BRIDGE_PORT:-8432}"
NAME="${IMESSAGE_BRIDGE_NAME:-$(hostname -s)}"
DB_PATH="$HOME/Library/Messages/chat.db"
REPO_URL="https://raw.githubusercontent.com/greghughespdx/imessage-bridge/main"

echo "Installing imessage-bridge..."

# Create install directory
mkdir -p "$INSTALL_DIR"

# Download bridge.py (or copy if running locally)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null || echo "")"
if [[ -f "$SCRIPT_DIR/bridge.py" ]]; then
    cp "$SCRIPT_DIR/bridge.py" "$INSTALL_DIR/bridge.py"
    echo "  Copied bridge.py from local directory"
else
    curl -sSL "$REPO_URL/bridge.py" -o "$INSTALL_DIR/bridge.py"
    echo "  Downloaded bridge.py"
fi

chmod +x "$INSTALL_DIR/bridge.py"

# Create launchd plist
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${INSTALL_DIR}/bridge.py</string>
        <string>--port</string>
        <string>${PORT}</string>
        <string>--name</string>
        <string>${NAME}</string>
        <string>--db</string>
        <string>${DB_PATH}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/bridge.log</string>
</dict>
</plist>
PLIST

echo "  Created launchd service"

# Unload if already loaded, then load
launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo "  Service started on port ${PORT}"

# Check if Full Disk Access is available
if ! python3 -c "import sqlite3; sqlite3.connect('file://$HOME/Library/Messages/chat.db?mode=ro', uri=True)" 2>/dev/null; then
    echo ""
    echo "  NOTE: Full Disk Access is required to read iMessage history."
    echo "  Open System Settings > Privacy & Security > Full Disk Access"
    echo "  and add Terminal (or your terminal app)."
    echo ""
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
else
    echo "  Full Disk Access: OK"
fi

echo ""
echo "Done! Bridge is running at http://localhost:${PORT}"
echo ""
echo "  Logs:      ${INSTALL_DIR}/bridge.log"
echo "  Uninstall: launchctl bootout gui/$(id -u)/${PLIST_NAME}"
echo "             rm -rf ${INSTALL_DIR} ${PLIST_PATH}"
