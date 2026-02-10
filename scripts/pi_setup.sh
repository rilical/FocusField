#!/usr/bin/env bash
set -euo pipefail

echo "=== FocusField Pi setup ==="

sudo apt update
sudo apt install -y \
  python3-pip python3-venv \
  python3-dev \
  build-essential \
  portaudio19-dev \
  v4l-utils \
  libatlas-base-dev

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -U pip
pip install -e .

echo "Done. Activate env with: source .venv/bin/activate"
