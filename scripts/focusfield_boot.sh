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
PYTHON_BIN=${FOCUSFIELD_PYTHON_BIN:-$ROOT_DIR/.venv/bin/python3}
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

export FOCUSFIELD_CONFIG_PATH="$CONFIG_PATH"
export FOCUSFIELD_CONFIG_EFFECTIVE="$CONFIG_EFFECTIVE"

eval "$("$PYTHON_BIN" scripts/boot_validation.py --config "$CONFIG_EFFECTIVE" --emit-shell-vars)"

read -r OUTPUT_SINK OUTPUT_EXACT_NAME < <("$PYTHON_BIN" - "$ROOT_DIR" "$CONFIG_EFFECTIVE" <<'PY'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "src"))

from focusfield.core.config import load_config

cfg = load_config(sys.argv[2])
output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}
if not isinstance(output_cfg, dict):
    print("", "")
    raise SystemExit(0)
sink = str(output_cfg.get("sink", "") or "").strip().lower()
usb_cfg = output_cfg.get("usb_mic", {})
if not isinstance(usb_cfg, dict):
    print(sink, "")
    raise SystemExit(0)
selector = usb_cfg.get("device_selector", {})
if not isinstance(selector, dict):
    print(sink, "")
    raise SystemExit(0)
print(sink, str(selector.get("exact_name", "") or "").strip())
PY
)

ENABLE_USB_GADGET=${FOCUSFIELD_ENABLE_USB_GADGET:-0}
if [[ "$OUTPUT_SINK" == "usb_mic" ]]; then
  ENABLE_USB_GADGET=1
fi
USB_CONNECTOR_PORT=${FOCUSFIELD_USB_GADGET_CONNECTOR_PORT:-unknown}
USB_REQUIRED_OUTPUT=${FOCUSFIELD_REQUIRE_USB_OUTPUT_EXACT_NAME:-${OUTPUT_EXACT_NAME:-${FOCUSFIELD_USB_GADGET_PRODUCT_NAME:-}}}
if [[ "$ENABLE_USB_GADGET" == "1" ]]; then
  feasibility_args=(
    appliance
    --connector-port "$USB_CONNECTOR_PORT"
  )
  if [[ -n "$USB_REQUIRED_OUTPUT" ]]; then
    feasibility_args+=(--require-output-device "$USB_REQUIRED_OUTPUT")
  fi
  if ! "$PYTHON_BIN" scripts/check_usb_demo_feasibility.py "${feasibility_args[@]}"; then
    echo "USB demo feasibility gate failed; refusing to start FocusField." >&2
    exit 5
  fi
fi

if [[ "$FOCUSFIELD_BOOT_AUDIO_ONLY" == "1" ]]; then
  CAMERA_SOURCE=auto
  CAMERA_SCOPE=any
  REQUIRE_CAMERAS=0
  REQUIRE_CHANNELS=1
fi

attempt=0
while :; do
  attempt=$((attempt + 1))
  preflight_args=(
    --config "$CONFIG_EFFECTIVE"
    --camera-source "$CAMERA_SOURCE"
    --camera-scope "$CAMERA_SCOPE"
    --require-audio-channels "$REQUIRE_CHANNELS"
  )
  if [[ "$FOCUSFIELD_BOOT_AUDIO_ONLY" == "1" ]]; then
    preflight_args+=(--audio-only)
  else
    preflight_args+=(--require-cameras "$REQUIRE_CAMERAS" --strict)
  fi
  if "$PYTHON_BIN" scripts/pi_preflight.py "${preflight_args[@]}"; then
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
  --config "$CONFIG_EFFECTIVE"
