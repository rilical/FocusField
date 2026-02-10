#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${1:-focusfield}
CONFIG_PATH=${2:-configs/full_3cam_8mic_pi.yaml}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing venv python at $PYTHON_BIN" >&2
  echo "Run: scripts/pi_setup.sh" >&2
  exit 1
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "Installing systemd unit: $UNIT_FILE"

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=FocusField pipeline
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
ExecStart=$PYTHON_BIN -m focusfield.main.run --config $CONFIG_PATH
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Done. Start with: sudo systemctl start $SERVICE_NAME"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"

