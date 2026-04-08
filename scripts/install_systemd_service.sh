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

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"

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
if ! FOCUSFIELD_SERVICE_USER="$SERVICE_USER" "$PYTHON_BIN" scripts/boot_validation.py --config "$CONFIG_PATH" --validate-local-models; then
  echo "Install validation failed: bundled model assets are missing or incomplete." >&2
  echo "Set local paths for the YuNet and mouth-landmark models before installing the service." >&2
  exit 1
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
BOOT_SCRIPT="$ROOT_DIR/scripts/focusfield_boot.sh"
if [[ ! -x "$BOOT_SCRIPT" ]]; then
  echo "Missing boot helper: $BOOT_SCRIPT" >&2
  exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  echo "User '$SERVICE_USER' does not exist." >&2
  exit 1
fi

echo "Installing systemd unit: $UNIT_FILE"

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=FocusField pipeline
After=multi-user.target
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service
EOF

if [[ "$FOCUSFIELD_BOOT_AUDIO_ONLY" != "1" ]]; then
  sudo tee -a "$UNIT_FILE" >/dev/null <<EOF
Wants=network-online.target
After=network-online.target
EOF
fi

sudo tee -a "$UNIT_FILE" >/dev/null <<EOF

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
User=$SERVICE_USER
Group=$SERVICE_USER
Environment=FOCUSFIELD_CAMERA_SOURCE=$BOOT_CAMERA_SOURCE
Environment=FOCUSFIELD_CAMERA_SCOPE=$BOOT_CAMERA_SCOPE
Environment=FOCUSFIELD_PRECHECK_RETRIES=$BOOT_RETRIES
Environment=FOCUSFIELD_PRECHECK_DELAY_SECONDS=$BOOT_DELAY_SECONDS
Environment=FOCUSFIELD_ENABLE_UMA8_LEDS=$BOOT_ENABLE_UMA8_LEDS
Environment=FOCUSFIELD_REQUIRE_LED_HID=$BOOT_REQUIRE_LED_HID
Environment=FOCUSFIELD_LED_VENDOR_ID=$BOOT_LED_VENDOR_ID
Environment=FOCUSFIELD_LED_PRODUCT_ID=$BOOT_LED_PRODUCT_ID
Environment=FOCUSFIELD_SERVICE_USER=$SERVICE_USER
Environment=OMP_NUM_THREADS=$BOOT_OMP_NUM_THREADS
Environment=OPENBLAS_NUM_THREADS=$BOOT_OPENBLAS_NUM_THREADS
Environment=MKL_NUM_THREADS=$BOOT_MKL_NUM_THREADS
Environment=PYTHONUNBUFFERED=1
ExecStart=$BOOT_SCRIPT $CONFIG_PATH
Restart=always
RestartSec=5
RestartPreventExitStatus=0
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Done. Start with: sudo systemctl start $SERVICE_NAME"
echo "Status with:  sudo systemctl status $SERVICE_NAME"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
