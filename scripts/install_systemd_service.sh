#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${1:-focusfield}
CONFIG_PATH=${2:-configs/meeting_peripheral.yaml}
SERVICE_USER=${3:-${SUDO_USER:-$(whoami)}}
BOOT_CAMERA_SOURCE=${FOCUSFIELD_CAMERA_SOURCE:-by-path}
BOOT_CAMERA_SCOPE=${FOCUSFIELD_CAMERA_SCOPE:-usb}
BOOT_RETRIES=${FOCUSFIELD_PRECHECK_RETRIES:-15}
BOOT_DELAY_SECONDS=${FOCUSFIELD_PRECHECK_DELAY_SECONDS:-5}
BOOT_ENABLE_UMA8_LEDS=${FOCUSFIELD_ENABLE_UMA8_LEDS:-}
BOOT_REQUIRE_LED_HID=${FOCUSFIELD_REQUIRE_LED_HID:-}
BOOT_LED_VENDOR_ID=${FOCUSFIELD_LED_VENDOR_ID:-}
BOOT_LED_PRODUCT_ID=${FOCUSFIELD_LED_PRODUCT_ID:-}
BOOT_OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}
BOOT_OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
BOOT_MKL_NUM_THREADS=${MKL_NUM_THREADS:-1}
BOOT_USB_GADGET_CONNECTOR_PORT=${FOCUSFIELD_USB_GADGET_CONNECTOR_PORT:-usb-c-otg}
BOOT_USB_GADGET_UDC=${FOCUSFIELD_USB_GADGET_UDC:-}
BOOT_USB_GADGET_VENDOR_ID=${FOCUSFIELD_USB_GADGET_VENDOR_ID:-0x1d6b}
BOOT_USB_GADGET_PRODUCT_ID=${FOCUSFIELD_USB_GADGET_PRODUCT_ID:-0x0104}
BOOT_USB_GADGET_SERIAL=${FOCUSFIELD_USB_GADGET_SERIAL:-FFDEMO0001}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN=${FOCUSFIELD_PYTHON_BIN:-$ROOT_DIR/.venv/bin/python3}
SYSTEMD_DIR=${FOCUSFIELD_SYSTEMD_DIR:-/etc/systemd/system}
SYSTEMCTL_BIN=${FOCUSFIELD_SYSTEMCTL_BIN:-systemctl}
if [[ ${FOCUSFIELD_SUDO_BIN+x} == x ]]; then
  SUDO_BIN=$FOCUSFIELD_SUDO_BIN
else
  SUDO_BIN=sudo
fi

run_root() {
  if [[ -n "$SUDO_BIN" ]]; then
    "$SUDO_BIN" "$@"
  else
    "$@"
  fi
}

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing venv python at $PYTHON_BIN" >&2
  echo "Run: scripts/pi_setup.sh" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config not found: $CONFIG_PATH" >&2
  exit 1
fi

eval "$("$PYTHON_BIN" scripts/boot_validation.py --config "$CONFIG_PATH" --emit-shell-vars)"
if ! "$PYTHON_BIN" scripts/boot_validation.py --config "$CONFIG_PATH" --validate-local-models; then
  echo "Install validation failed: bundled model assets are missing or incomplete." >&2
  echo "Set local paths for the YuNet and mouth-landmark models before installing the service." >&2
  exit 1
fi

read -r USB_OUTPUT_SINK USB_OUTPUT_EXACT_NAME < <("$PYTHON_BIN" - "$ROOT_DIR" "$CONFIG_PATH" <<'PY'
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
BOOT_USB_GADGET_PRODUCT_NAME=${FOCUSFIELD_USB_GADGET_PRODUCT_NAME:-${USB_OUTPUT_EXACT_NAME:-FocusField USB Mic}}
BOOT_REQUIRE_USB_OUTPUT_EXACT_NAME=${FOCUSFIELD_REQUIRE_USB_OUTPUT_EXACT_NAME:-${USB_OUTPUT_EXACT_NAME:-$BOOT_USB_GADGET_PRODUCT_NAME}}
BOOT_ENABLE_USB_GADGET=0
if [[ "$USB_OUTPUT_SINK" == "usb_mic" ]]; then
  BOOT_ENABLE_USB_GADGET=1
fi

UNIT_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
GADGET_UNIT_NAME="${SERVICE_NAME}-usb-gadget"
GADGET_UNIT_FILE="${SYSTEMD_DIR}/${GADGET_UNIT_NAME}.service"
BOOT_SCRIPT="$ROOT_DIR/scripts/focusfield_boot.sh"
GADGET_SCRIPT="$ROOT_DIR/scripts/setup_usb_gadget_mic.sh"
if [[ ! -x "$BOOT_SCRIPT" ]]; then
  echo "Missing boot helper: $BOOT_SCRIPT" >&2
  exit 1
fi
if [[ "$BOOT_ENABLE_USB_GADGET" == "1" && ! -x "$GADGET_SCRIPT" ]]; then
  echo "Missing USB gadget helper: $GADGET_SCRIPT" >&2
  exit 1
fi
if [[ "$BOOT_ENABLE_USB_GADGET" == "1" && "$BOOT_USB_GADGET_CONNECTOR_PORT" == "usb-a-host" ]]; then
  echo "Configured connector port is host-only (${BOOT_USB_GADGET_CONNECTOR_PORT}); refusing to install an impossible USB gadget path." >&2
  exit 5
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  echo "User '$SERVICE_USER' does not exist." >&2
  exit 1
fi

echo "Installing systemd unit: $UNIT_FILE"

run_root tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=FocusField pipeline
After=multi-user.target
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service
EOF

if [[ "$BOOT_ENABLE_USB_GADGET" == "1" ]]; then
  run_root tee -a "$UNIT_FILE" >/dev/null <<EOF
Requires=${GADGET_UNIT_NAME}.service
After=${GADGET_UNIT_NAME}.service
EOF
fi

if [[ "$FOCUSFIELD_BOOT_AUDIO_ONLY" != "1" ]]; then
  run_root tee -a "$UNIT_FILE" >/dev/null <<EOF
Wants=network-online.target
After=network-online.target
EOF
fi

run_root tee -a "$UNIT_FILE" >/dev/null <<EOF

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
User=$SERVICE_USER
Group=$SERVICE_USER
Environment="FOCUSFIELD_CAMERA_SOURCE=$BOOT_CAMERA_SOURCE"
Environment="FOCUSFIELD_CAMERA_SCOPE=$BOOT_CAMERA_SCOPE"
Environment="FOCUSFIELD_PRECHECK_RETRIES=$BOOT_RETRIES"
Environment="FOCUSFIELD_PRECHECK_DELAY_SECONDS=$BOOT_DELAY_SECONDS"
Environment="FOCUSFIELD_ENABLE_UMA8_LEDS=$BOOT_ENABLE_UMA8_LEDS"
Environment="FOCUSFIELD_REQUIRE_LED_HID=$BOOT_REQUIRE_LED_HID"
Environment="FOCUSFIELD_LED_VENDOR_ID=$BOOT_LED_VENDOR_ID"
Environment="FOCUSFIELD_LED_PRODUCT_ID=$BOOT_LED_PRODUCT_ID"
Environment="OMP_NUM_THREADS=$BOOT_OMP_NUM_THREADS"
Environment="OPENBLAS_NUM_THREADS=$BOOT_OPENBLAS_NUM_THREADS"
Environment="MKL_NUM_THREADS=$BOOT_MKL_NUM_THREADS"
Environment="PYTHONUNBUFFERED=1"
Environment="FOCUSFIELD_ENABLE_USB_GADGET=$BOOT_ENABLE_USB_GADGET"
Environment="FOCUSFIELD_REQUIRE_USB_OUTPUT_EXACT_NAME=$BOOT_REQUIRE_USB_OUTPUT_EXACT_NAME"
Environment="FOCUSFIELD_USB_GADGET_CONNECTOR_PORT=$BOOT_USB_GADGET_CONNECTOR_PORT"
Environment="FOCUSFIELD_USB_GADGET_PRODUCT_NAME=$BOOT_USB_GADGET_PRODUCT_NAME"
Environment="FOCUSFIELD_USB_GADGET_UDC=$BOOT_USB_GADGET_UDC"
Environment="FOCUSFIELD_USB_GADGET_VENDOR_ID=$BOOT_USB_GADGET_VENDOR_ID"
Environment="FOCUSFIELD_USB_GADGET_PRODUCT_ID=$BOOT_USB_GADGET_PRODUCT_ID"
Environment="FOCUSFIELD_USB_GADGET_SERIAL=$BOOT_USB_GADGET_SERIAL"
ExecStart=$BOOT_SCRIPT $CONFIG_PATH
Restart=always
RestartSec=5
RestartPreventExitStatus=0 5
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

if [[ "$BOOT_ENABLE_USB_GADGET" == "1" ]]; then
  echo "Installing USB gadget unit: $GADGET_UNIT_FILE"
  run_root tee "$GADGET_UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=FocusField USB gadget microphone
DefaultDependencies=no
After=local-fs.target systemd-modules-load.service
Before=${SERVICE_NAME}.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment="FOCUSFIELD_USB_GADGET_CONNECTOR_PORT=$BOOT_USB_GADGET_CONNECTOR_PORT"
Environment="FOCUSFIELD_USB_GADGET_PRODUCT_NAME=$BOOT_USB_GADGET_PRODUCT_NAME"
Environment="FOCUSFIELD_USB_GADGET_UDC=$BOOT_USB_GADGET_UDC"
Environment="FOCUSFIELD_USB_GADGET_VENDOR_ID=$BOOT_USB_GADGET_VENDOR_ID"
Environment="FOCUSFIELD_USB_GADGET_PRODUCT_ID=$BOOT_USB_GADGET_PRODUCT_ID"
Environment="FOCUSFIELD_USB_GADGET_SERIAL=$BOOT_USB_GADGET_SERIAL"
ExecStart=$GADGET_SCRIPT up
ExecStop=$GADGET_SCRIPT down

[Install]
WantedBy=multi-user.target
EOF
else
  if [[ -f "$GADGET_UNIT_FILE" ]]; then
    run_root "$SYSTEMCTL_BIN" disable --now "$GADGET_UNIT_NAME" || true
    run_root rm -f "$GADGET_UNIT_FILE"
  fi
fi

run_root "$SYSTEMCTL_BIN" daemon-reload
run_root "$SYSTEMCTL_BIN" enable "$SERVICE_NAME"
if [[ "$BOOT_ENABLE_USB_GADGET" == "1" ]]; then
  run_root "$SYSTEMCTL_BIN" enable "$GADGET_UNIT_NAME"
fi

echo "Done. Start with: sudo systemctl start $SERVICE_NAME"
echo "Status with:  sudo systemctl status $SERVICE_NAME"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
