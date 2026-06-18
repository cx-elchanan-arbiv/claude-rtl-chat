#!/bin/bash
# Legacy fallback: re-render every second without launchd.
# (serve.py is the primary path and already does this in-process.)
DIR="$(cd "$(dirname "$0")" && pwd)"
while true; do
  python3 "$DIR/extract.py" 2>/dev/null
  sleep 1
done
