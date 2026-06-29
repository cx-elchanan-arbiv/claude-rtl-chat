#!/bin/bash
# Safely restart the RTL-chat server (launchd: com.elchanan.rtl-chat).
#
# Safe to run FROM INSIDE the chat: the restart is detached from this process, and
# serve.py now spawns `claude` with start_new_session=True, so killing/relaunching
# serve.py no longer kills the turn that issued the command — the in-flight reply
# keeps running and lands in the transcript; the new server renders it on next scan.
#
# Usage:  bash restart.sh
set -euo pipefail

LABEL="com.elchanan.rtl-chat"
UID_NUM="$(id -u)"

# `kickstart -k` = kill the running job (if any) then start it fresh under launchd.
# setsid fully detaches the restart from the caller so it survives serve.py going down.
setsid launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || \
  launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"

echo "↻ restarted ${LABEL} — give it ~1s, then reload http://127.0.0.1:7778"
