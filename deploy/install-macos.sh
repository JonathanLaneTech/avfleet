#!/bin/bash
# Install avfleet as a launchd agent on macOS.
# Usage (from the repo root):  bash deploy/install-macos.sh [path/to/config.toml]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$(cd "$(dirname "${1:-$HOME/.avfleet/config.toml}")" && pwd)/$(basename "${1:-config.toml}")"
HOME_DIR="$HOME/.avfleet"
VENV="$HOME_DIR/venv"
LOGDIR="$HOME/Library/Logs"
LABEL="com.example.avfleet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || true)"
if [ -z "$PY" ]; then
  echo "Python 3.11+ is required (e.g. 'brew install python@3.13')." >&2
  exit 1
fi

mkdir -p "$HOME_DIR" "$LOGDIR" "$(dirname "$PLIST")"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$REPO"

if [ ! -f "$CONFIG" ]; then
  echo "No config at $CONFIG. Copy config.example.toml there and edit it first." >&2
  exit 1
fi
touch "$HOME_DIR/secrets.env" && chmod 600 "$HOME_DIR/secrets.env"

sed -e "s|__VENV__|$VENV|" -e "s|__CONFIG__|$CONFIG|" -e "s|__LOGDIR__|$LOGDIR|g" \
  "$REPO/deploy/$LABEL.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "avfleet running. Dashboard: http://$(scutil --get LocalHostName 2>/dev/null || hostname).local:8080"
echo "Logs: $LOGDIR/avfleet.log"
