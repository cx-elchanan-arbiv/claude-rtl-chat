#!/usr/bin/env bash
# Remove the Claude RTL Chat launchd agent (macOS).
LABEL="com.user.rtl-chat"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
  || launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ Uninstalled. (Generated files in this folder were left untouched.)"
