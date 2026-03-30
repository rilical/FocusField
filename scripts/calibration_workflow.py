#!/usr/bin/env python3
"""Guided calibration status and workflow planner for FocusField."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.core.config import load_config
from focusfield.vision.calibration.runtime_overlay import get_camera_calibration_path, load_camera_calibration
from scripts.pi_preflight import _load_mic_profile_yaw


def build_calibration_plan(config_path: str) -> Dict[str, Any]:
    config_file = Path(config_path).expanduser().resolve()
    config = load_config(str(config_file))
    calibration, meta = load_camera_calibration(config, base_dir=config_file.parent)
    profile_name, mic_yaw = _load_mic_profile_yaw(config)
    cameras = calibration.get("cameras", []) if isinstance(calibration, dict) else []
    modified_ids = meta.get("modified_camera_ids", []) if isinstance(meta, dict) else []
    camera_path = get_camera_calibration_path(base_dir=config_file.parent)
    needs_mic_calibration = mic_yaw is None or abs(float(mic_yaw)) < 1e-6
    needs_camera_calibration = (not camera_path.exists()) or len(modified_ids) < len(cameras)
    steps: List[str] = []
    if needs_mic_calibration:
        steps.append("Run scripts/calibrate_uma8.py and update configs/device_profiles.yaml.")
    if needs_camera_calibration:
        steps.append("Open the live UI calibration endpoint and save camera_calibration.json for all configured cameras.")
    if not steps:
        steps.append("Calibration artifacts look present; verify alignment with a short smoke run.")
    return {
        "config_path": str(config_file),
        "camera_calibration_path": str(camera_path),
        "camera_calibration_exists": camera_path.exists(),
        "camera_ids": [str(item.get("id", "")) for item in cameras if isinstance(item, dict)],
        "modified_camera_ids": [str(item) for item in modified_ids],
        "mic_profile": profile_name,
        "mic_yaw_offset_deg": mic_yaw,
        "needs_mic_calibration": bool(needs_mic_calibration),
        "needs_camera_calibration": bool(needs_camera_calibration),
        "steps": steps,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guided FocusField calibration workflow")
    parser.add_argument("--config", default="configs/meeting_peripheral.yaml", help="Config path")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    payload = build_calibration_plan(args.config)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
