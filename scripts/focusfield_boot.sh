#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config_path>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="$1"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing venv python at $PYTHON_BIN" >&2
  echo "Run: scripts/pi_setup.sh" >&2
  exit 2
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config file not found: $CONFIG_PATH" >&2
  exit 3
fi

MAX_RETRIES=${FOCUSFIELD_PRECHECK_RETRIES:-15}
DELAY_SECONDS=${FOCUSFIELD_PRECHECK_DELAY_SECONDS:-5}
CAMERA_SOURCE=${FOCUSFIELD_CAMERA_SOURCE:-by-path}
CAMERA_SCOPE=${FOCUSFIELD_CAMERA_SCOPE:-usb}
REQUIRE_CAMERAS=${FOCUSFIELD_REQUIRE_CAMERAS:-3}
REQUIRE_CHANNELS=${FOCUSFIELD_REQUIRE_AUDIO_CHANNELS:-8}

attempt=0
while :; do
  attempt=$((attempt + 1))
  if "$PYTHON_BIN" scripts/pi_preflight.py \
    --config "$CONFIG_PATH" \
    --camera-source "$CAMERA_SOURCE" \
    --camera-scope "$CAMERA_SCOPE" \
    --require-cameras "$REQUIRE_CAMERAS" \
    --require-audio-channels "$REQUIRE_CHANNELS" \
    --strict; then
    break
  fi

  if (( attempt >= MAX_RETRIES )); then
    echo "Preflight did not pass after ${MAX_RETRIES} attempts; exiting for systemd retry." >&2
    exit 4
  fi

  echo "Preflight failed on attempt ${attempt}/${MAX_RETRIES}; retrying in ${DELAY_SECONDS}s..."
  sleep "$DELAY_SECONDS"
done

"$PYTHON_BIN" -m focusfield.main.run \
  --config "$CONFIG_PATH" \
  --mode vision
