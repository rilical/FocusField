#!/usr/bin/env python3
"""Guided provisioning workflow for FocusField field units."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from scripts.boot_validation import boot_plan, load_effective_config, validate_local_model_assets


def build_provision_plan(
    config_path: str,
    service_name: str = "focusfield",
    replacement: bool = False,
) -> Dict[str, Any]:
    config_file = Path(config_path).expanduser().resolve()
    config = load_effective_config(str(config_file))
    plan = boot_plan(config)
    preflight_cmd = [
        "scripts/pi_preflight.py",
        "--config",
        str(config_file),
        "--camera-source",
        str(plan["camera_source"]),
        "--camera-scope",
        str(plan["camera_scope"]),
        "--require-audio-channels",
        str(plan["require_audio_channels"]),
    ]
    if plan["audio_only"]:
        preflight_cmd.append("--audio-only")
    else:
        preflight_cmd.extend(["--require-cameras", str(plan["require_cameras"]), "--strict"])
    install_cmd = ["scripts/install_systemd_service.sh", service_name, str(config_file)]
    return {
        "config_path": str(config_file),
        "service_name": service_name,
        "replacement": bool(replacement),
        "mode": str(plan["mode"]),
        "audio_only_boot": bool(plan["audio_only"]),
        "validate_local_models": True,
        "commands": {
            "preflight": preflight_cmd,
            "install_service": install_cmd,
        },
        "steps": [
            "validate_local_models",
            "run_preflight",
            "install_service",
        ],
    }


def execute_provision_plan(
    plan: Dict[str, Any],
    run_preflight: bool = False,
    install_service: bool = False,
    python_bin: str = sys.executable,
    root_dir: Optional[str] = None,
) -> Dict[str, Any]:
    config_path = str(plan.get("config_path", ""))
    config = load_effective_config(config_path)
    errors = validate_local_model_assets(config, config_path)
    result: Dict[str, Any] = {
        "config_path": config_path,
        "service_name": str(plan.get("service_name", "focusfield")),
        "validate_local_models": {"passed": len(errors) == 0, "errors": errors},
        "preflight": None,
        "install_service": None,
        "passed": len(errors) == 0,
    }
    cwd = Path(root_dir).resolve() if root_dir else Path(__file__).resolve().parents[1]
    if run_preflight and result["passed"]:
        result["preflight"] = _run_script([python_bin, *plan["commands"]["preflight"]], cwd)
        result["passed"] = result["passed"] and bool(result["preflight"]["passed"])
    if install_service and result["passed"]:
        result["install_service"] = _run_script(["bash", *plan["commands"]["install_service"]], cwd)
        result["passed"] = result["passed"] and bool(result["install_service"]["passed"])
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guided FocusField provisioning workflow")
    parser.add_argument("--config", default="configs/meeting_peripheral.yaml", help="Config path")
    parser.add_argument("--service-name", default="focusfield", help="Systemd service name")
    parser.add_argument("--replacement", action="store_true", help="Mark this as a replacement/reprovision workflow")
    parser.add_argument("--run-preflight", action="store_true", help="Run pi_preflight as part of provisioning")
    parser.add_argument("--install-service", action="store_true", help="Run install_systemd_service as part of provisioning")
    parser.add_argument("--json", action="store_true", help="Emit JSON result/plan")
    args = parser.parse_args(argv)

    plan = build_provision_plan(args.config, service_name=args.service_name, replacement=args.replacement)
    result = execute_provision_plan(
        plan,
        run_preflight=args.run_preflight,
        install_service=args.install_service,
    )
    payload = result if (args.run_preflight or args.install_service) else plan
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2))
    if args.run_preflight or args.install_service:
        return 0 if bool(result["passed"]) else 1
    return 0


def _run_script(cmd: List[str], cwd: Path) -> Dict[str, Any]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": cmd,
    }


if __name__ == "__main__":
    raise SystemExit(main())
