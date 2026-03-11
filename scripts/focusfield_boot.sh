#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config_path>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"

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
ENABLE_UMA8_LEDS=${FOCUSFIELD_ENABLE_UMA8_LEDS:-}
REQUIRE_LED_HID_ENV=${FOCUSFIELD_REQUIRE_LED_HID:-}
LED_VENDOR_ID_ENV=${FOCUSFIELD_LED_VENDOR_ID:-}
LED_PRODUCT_ID_ENV=${FOCUSFIELD_LED_PRODUCT_ID:-}

CONFIG_EFFECTIVE="$CONFIG_PATH"
if [[ -n "$ENABLE_UMA8_LEDS" ]]; then
  CONFIG_EFFECTIVE=$(mktemp /tmp/focusfield-config.XXXXXX.yaml)
  "$PYTHON_BIN" - "$CONFIG_PATH" "$CONFIG_EFFECTIVE" "$ENABLE_UMA8_LEDS" <<'PY'
import sys
from pathlib import Path
import yaml

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
raw = str(sys.argv[3]).strip().lower()
enabled = raw in {"1", "true", "yes", "on"}

cfg = yaml.safe_load(src.read_text()) or {}
if not isinstance(cfg, dict):
    cfg = {}
uma8 = cfg.get("uma8_leds")
if not isinstance(uma8, dict):
    uma8 = {}
uma8["enabled"] = enabled
cfg["uma8_leds"] = uma8
dst.write_text(yaml.safe_dump(cfg, sort_keys=False))
print(dst)
PY
  echo "Using runtime LED override: uma8_leds.enabled=${ENABLE_UMA8_LEDS} (${CONFIG_EFFECTIVE})"
fi

LED_PRECHECK=$("$PYTHON_BIN" - "$CONFIG_EFFECTIVE" "$REQUIRE_LED_HID_ENV" "$LED_VENDOR_ID_ENV" "$LED_PRODUCT_ID_ENV" <<'PY'
import sys
from pathlib import Path
import yaml

cfg_path = Path(sys.argv[1])
require_env = str(sys.argv[2]).strip().lower()
vendor_env = str(sys.argv[3]).strip()
product_env = str(sys.argv[4]).strip()

cfg = yaml.safe_load(cfg_path.read_text()) or {}
if not isinstance(cfg, dict):
    cfg = {}
runtime = cfg.get("runtime", {})
if not isinstance(runtime, dict):
    runtime = {}
req = runtime.get("requirements", {})
if not isinstance(req, dict):
    req = {}
uma8 = cfg.get("uma8_leds", {})
if not isinstance(uma8, dict):
    uma8 = {}

require_led_hid = False
if require_env:
    require_led_hid = require_env in {"1", "true", "yes", "on"}
else:
    require_led_hid = bool(req.get("require_led_hid", False)) or (
        bool(uma8.get("enabled", False)) and bool(uma8.get("strict_transport", False))
    )

vendor_id = int(vendor_env) if vendor_env else int(uma8.get("vendor_id", 0x2752) or 0x2752)
product_id = int(product_env) if product_env else int(uma8.get("product_id", 0x001C) or 0x001C)
print(f"{int(require_led_hid)} {vendor_id} {product_id}")
PY
)
read -r REQUIRE_LED_HID LED_VENDOR_ID LED_PRODUCT_ID <<<"$LED_PRECHECK"

RT_SETTINGS=$("$PYTHON_BIN" - "$CONFIG_EFFECTIVE" <<'PY'
import sys
from pathlib import Path
import yaml

cfg = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
if not isinstance(cfg, dict):
    cfg = {}
runtime = cfg.get("runtime", {})
if not isinstance(runtime, dict):
    runtime = {}
scheduling = runtime.get("scheduling", {})
if not isinstance(scheduling, dict):
    scheduling = {}
enable_rt = bool(scheduling.get("enable_rt", False))
try:
    rt_priority = int(scheduling.get("rt_priority", 70) or 70)
except Exception:
    rt_priority = 70
rt_priority = max(1, min(99, rt_priority))
print(f"{int(enable_rt)} {rt_priority}")
PY
)
read -r ENABLE_RT RT_PRIORITY <<<"$RT_SETTINGS"

attempt=0
while :; do
  attempt=$((attempt + 1))
  PRECHECK_CMD=(
    "$PYTHON_BIN" scripts/pi_preflight.py
    --config "$CONFIG_EFFECTIVE" \
    --camera-source "$CAMERA_SOURCE" \
    --camera-scope "$CAMERA_SCOPE" \
    --require-cameras "$REQUIRE_CAMERAS" \
    --require-audio-channels "$REQUIRE_CHANNELS" \
    --strict
  )
  if [[ "${REQUIRE_LED_HID}" == "1" ]]; then
    PRECHECK_CMD+=(--require-led-hid --led-vendor-id "$LED_VENDOR_ID" --led-product-id "$LED_PRODUCT_ID")
  fi
  if "${PRECHECK_CMD[@]}"; then
    break
  fi

  if (( attempt >= MAX_RETRIES )); then
    echo "Preflight did not pass after ${MAX_RETRIES} attempts; exiting for systemd retry." >&2
    exit 4
  fi

  echo "Preflight failed on attempt ${attempt}/${MAX_RETRIES}; retrying in ${DELAY_SECONDS}s..."
  sleep "$DELAY_SECONDS"
done

child_pid=""
_forward_term() {
  if [[ -n "${child_pid}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -INT "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 130
}
trap _forward_term INT TERM

RUN_PREFIX=()
if [[ "${ENABLE_RT}" == "1" ]]; then
  if command -v chrt >/dev/null 2>&1; then
    if chrt -r "$RT_PRIORITY" true >/dev/null 2>&1; then
      RUN_PREFIX=(chrt -r "$RT_PRIORITY")
      echo "RT scheduler enabled: chrt -r ${RT_PRIORITY}"
    else
      echo "WARN: RT scheduler requested but chrt permissions are insufficient; continuing without RT." >&2
    fi
  else
    echo "WARN: RT scheduler requested but chrt is unavailable; continuing without RT." >&2
  fi
fi

"${RUN_PREFIX[@]}" "$PYTHON_BIN" -m focusfield.main.run \
  --config "$CONFIG_EFFECTIVE" \
  --mode vision &
child_pid=$!

set +e
wait "$child_pid"
exit_code=$?
set -e

trap - INT TERM
exit "$exit_code"
