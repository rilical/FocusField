#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: scripts/pi_demo_launch.sh [--mode live|observe] [--output CONFIG]

Stops the FocusField service, kills stale foreground runs, regenerates the
Pi-local config from the chosen demo base profile, runs strict preflight once,
then launches FocusField in the foreground.
EOF
}

MODE="live"
OUTPUT_CONFIG="configs/full_3cam_working_local.yaml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --output)
      OUTPUT_CONFIG="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  live)
    BASE_CONFIG="configs/full_3cam_8mic_pi_demo_live.yaml"
    ;;
  observe)
    BASE_CONFIG="configs/full_3cam_8mic_pi_demo_observe.yaml"
    ;;
  *)
    echo "Unsupported mode: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac

PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing venv python at $PYTHON_BIN" >&2
  echo "Run scripts/pi_setup.sh first." >&2
  exit 3
fi

echo "Stopping service and stale FocusField processes..."
sudo systemctl stop focusfield >/dev/null 2>&1 || true
pkill -f 'python3 -m focusfield.main.run' >/dev/null 2>&1 || true
pkill -f 'python3 scripts/pi_preflight.py' >/dev/null 2>&1 || true
sleep 2

LOCK_PATH=${FOCUSFIELD_LOCK_PATH:-/tmp/focusfield-runtime.lock}
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Another FocusField runtime is already active (lock: $LOCK_PATH)." >&2
  exit 5
fi

echo "Generating local config from $BASE_CONFIG"
"$PYTHON_BIN" scripts/prepare_pi_local_config.py \
  --base-config "$BASE_CONFIG" \
  --output "$OUTPUT_CONFIG" \
  --camera-source by-path \
  --camera-scope usb \
  --max-cameras 3 \
  --require-cameras 3 \
  --require-audio-channels 8 \
  --strict

echo "Running strict preflight once..."
"$PYTHON_BIN" scripts/pi_preflight.py \
  --config "$OUTPUT_CONFIG" \
  --camera-source by-path \
  --camera-scope usb \
  --require-cameras 3 \
  --require-audio-channels 8 \
  --strict

echo "Pi IPs:"
hostname -I || true

echo "Launching FocusField with $OUTPUT_CONFIG"
exec "$PYTHON_BIN" -m focusfield.main.run --config "$OUTPUT_CONFIG"
