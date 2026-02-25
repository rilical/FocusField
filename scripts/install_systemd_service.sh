#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${1:-focusfield}
CONFIG_PATH=${2:-configs/full_3cam_8mic_pi.yaml}
SERVICE_USER=${3:-${SUDO_USER:-$(whoami)}}

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
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
User=$SERVICE_USER
Group=$SERVICE_USER
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
