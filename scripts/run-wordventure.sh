#!/usr/bin/env bash
# Launches the development build against a local artel-mcp (default 127.0.0.1:4320).
#   scripts/run-wordventure.sh [app] [host:port]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$HERE/../../builds/WordVenture-dev.app}"
SERVER="${2:-127.0.0.1:4320}"
BIN="$APP/Contents/MacOS/$(basename "$APP" .app | sed 's/-dev$//')"
ARTEL_SDK_TOKEN=local exec "$BIN" -artel-server "$SERVER" -artel-secure false -artel-project local -screen-width 1280 -screen-height 720
