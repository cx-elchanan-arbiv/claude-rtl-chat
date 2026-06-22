#!/usr/bin/env bash
# Install Claude RTL Chat as a launchd agent (macOS).
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "❌ python3 not found"; exit 1; }

# Optional dependency for the real Max-usage % feature (macOS + Chrome only).
# The core mirror works without it.
"$PY" -m pip install --quiet --break-system-packages pycryptodome 2>/dev/null \
  || "$PY" -m pip install --quiet --user pycryptodome 2>/dev/null \
  || echo "⚠️  couldn't install pycryptodome — token mirror still works, Max% disabled"

LABEL="com.user.rtl-chat"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DIR/serve.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/rtl-view.log</string>
    <key>StandardErrorPath</key><string>/tmp/rtl-view.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo "✅ Installed. Open http://127.0.0.1:7778"
echo "ℹ️  For the Max-usage %, when macOS asks for 'Chrome Safe Storage' click ALWAYS ALLOW."
