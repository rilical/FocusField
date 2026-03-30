#!/usr/bin/env python3
"""Build a recovery plan from recent FocusField artifacts."""

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

from scripts.calibration_workflow import build_calibration_plan
from scripts.support_bundle import resolve_run_dir


def build_recovery_plan(
    config_path: str,
    run_dir: str = "artifacts/LATEST",
    service_name: str = "focusfield",
) -> Dict[str, Any]:
    run_path = resolve_run_dir(run_dir)
    calibration = build_calibration_plan(config_path)
    issues: List[str] = []
    actions: List[str] = [
        f"sudo systemctl status {service_name}",
        f"sudo systemctl restart {service_name}",
        f"python3 scripts/support_bundle.py --config {config_path} --run-dir {run_dir} --output support_bundle.zip",
    ]

    latest_perf = _read_last_jsonl_entry(run_path / "logs" / "perf.jsonl")
    latest_event = _read_last_jsonl_entry(run_path / "logs" / "events.jsonl")

    if not run_path.exists():
        issues.append("artifacts_missing")
        actions.append(f"python3 scripts/provision_focusfield.py --config {config_path} --run-preflight")
    if calibration.get("needs_mic_calibration"):
        issues.append("mic_calibration_missing")
        actions.append(f"python3 scripts/calibration_workflow.py --config {config_path}")
    if calibration.get("needs_camera_calibration"):
        issues.append("camera_calibration_missing")
        actions.append(f"python3 scripts/calibration_workflow.py --config {config_path}")

    if isinstance(latest_perf, dict):
        queue_pressure = latest_perf.get("queue_pressure", {})
        if isinstance(queue_pressure, dict) and int(queue_pressure.get("drop_total_window", 0) or 0) > 0:
            issues.append("queue_pressure")
            actions.append("Investigate overload and run the production perf gate before returning the unit to service.")
        audio_output = latest_perf.get("audio_output", latest_perf.get("output", {}))
        if isinstance(audio_output, dict) and int(audio_output.get("underrun_total", 0) or 0) > 0:
            issues.append("output_underrun")
            actions.append("Check host-facing output device stability and validate reconnect behavior.")
    if isinstance(latest_event, dict):
        context = latest_event.get("context", {})
        if isinstance(context, dict):
            event = str(context.get("event", "") or "")
            if event in {"camera_missing", "camera_disconnected"}:
                issues.append("camera_path")
                actions.append(f"python3 scripts/provision_focusfield.py --config {config_path} --run-preflight")

    return {
        "config_path": str(Path(config_path).expanduser().resolve()),
        "run_dir": str(run_path),
        "service_name": service_name,
        "issues": issues,
        "actions": actions,
        "latest_perf": latest_perf,
        "latest_event": latest_event,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a FocusField recovery plan from the latest artifacts")
    parser.add_argument("--config", default="configs/meeting_peripheral.yaml", help="Config path")
    parser.add_argument("--run-dir", default="artifacts/LATEST", help="Run dir or LATEST pointer")
    parser.add_argument("--service-name", default="focusfield", help="Service name")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    payload = build_recovery_plan(args.config, run_dir=args.run_dir, service_name=args.service_name)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2))
    return 0


def _read_last_jsonl_entry(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    last: Optional[Dict[str, Any]] = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if isinstance(payload, dict):
                last = payload
    return last


if __name__ == "__main__":
    raise SystemExit(main())
