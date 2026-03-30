#!/usr/bin/env python3
"""Host-facing meeting gate for FocusField.

This gate separates unproven assumptions from proven hardware evidence:
- `--dry-run` emits an expected verdict shape with assumptions only.
- `--evidence-json` validates a machine-readable evidence bundle and fails closed
  when host-visible microphone proof, reconnect proof, or meeting-app verdict
  artifacts are missing or malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

if __package__:
    from .boot_validation import boot_plan, load_effective_config
else:  # pragma: no cover - direct script execution
    from boot_validation import boot_plan, load_effective_config


DEFAULT_MEETING_APPS = ("Zoom", "Google Meet", "Microsoft Teams")
REQUIRED_MEETING_APP_KEYS = ("app", "artifact_path", "selected_input_device", "duration_s", "verdict")
PROVEN_STATUS = "PROVEN"
ASSUMED_STATUS = "ASSUMED"
FAILED_STATUS = "FAILED"
DRY_RUN_STATUS = "DRY_RUN"
NO_EVIDENCE_STATUS = "NO_EVIDENCE"


def load_host_gate_evidence(path: str | Path) -> Dict[str, Any]:
    evidence_path = Path(path).expanduser()
    with evidence_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("meeting_host_gate evidence must be a JSON object")
    return data


def evaluate_host_gate(
    config: Dict[str, Any],
    evidence: Optional[Dict[str, Any]] = None,
    *,
    dry_run: bool = False,
    required_apps: Sequence[str] = DEFAULT_MEETING_APPS,
) -> Dict[str, Any]:
    runtime_plan = boot_plan(config)
    app_requirements = tuple(required_apps) if required_apps else DEFAULT_MEETING_APPS
    report: Dict[str, Any] = {
        "mode": runtime_plan["mode"],
        "dry_run": bool(dry_run),
        "source": "dry_run" if dry_run else ("evidence" if evidence is not None else "none"),
        "passed": False,
        "status": DRY_RUN_STATUS if dry_run else NO_EVIDENCE_STATUS,
        "boot_plan": runtime_plan,
        "checks": [],
        "proven_checks": [],
        "assumptions": [],
        "validation_errors": [],
        "meeting_app_artifact_shape": {
            "required_keys": list(REQUIRED_MEETING_APP_KEYS),
            "required_apps": list(app_requirements),
        },
    }

    if dry_run:
        report["assumptions"] = [
            "cold_boot_host_visible_mic not proven",
            "reconnect_recovery not proven",
            "meeting_app_verdicts not proven",
        ]
        report["checks"] = [
            _assumed_check(
                "cold_boot_host_visible_mic",
                "Host-visible microphone must be proven on real hardware after cold boot.",
            ),
            _assumed_check(
                "reconnect_recovery",
                "Replug recovery must be proven on real hardware.",
            ),
            _assumed_check(
                "meeting_app_verdict_artifacts",
                "Meeting-app verdict artifacts must be proven for Zoom, Google Meet, and Microsoft Teams.",
            ),
        ]
        return report

    if evidence is None:
        report["validation_errors"].append("evidence_json is required unless --dry-run is set")
        report["checks"] = [
            _failed_check(
                "cold_boot_host_visible_mic",
                "evidence required",
                "Provide a host-gate evidence JSON bundle or use --dry-run.",
            ),
            _failed_check(
                "reconnect_recovery",
                "evidence required",
                "Provide a host-gate evidence JSON bundle or use --dry-run.",
            ),
            _failed_check(
                "meeting_app_verdict_artifacts",
                "evidence required",
                "Provide a host-gate evidence JSON bundle or use --dry-run.",
            ),
        ]
        return report

    evidence_errors: List[str] = []
    checks: List[Dict[str, Any]] = []

    cold_boot = evidence.get("cold_boot")
    if not isinstance(cold_boot, dict):
        evidence_errors.append("cold_boot must be a JSON object")
        checks.append(_failed_check("cold_boot_host_visible_mic", "missing cold_boot object", "cold_boot must be an object"))
    else:
        checks.append(_evaluate_cold_boot(cold_boot))

    reconnect = evidence.get("reconnect")
    if not isinstance(reconnect, dict):
        evidence_errors.append("reconnect must be a JSON object")
        checks.append(_failed_check("reconnect_recovery", "missing reconnect object", "reconnect must be an object"))
    else:
        checks.append(_evaluate_reconnect(reconnect))

    meeting_apps = evidence.get("meeting_apps")
    if not isinstance(meeting_apps, list):
        evidence_errors.append("meeting_apps must be a JSON array")
        checks.append(_failed_check("meeting_app_verdict_artifacts", "missing meeting_apps array", "meeting_apps must be an array"))
    else:
        app_check, app_errors = _evaluate_meeting_app_verdicts(meeting_apps, app_requirements)
        checks.append(app_check)
        evidence_errors.extend(app_errors)

    report["checks"] = checks
    report["validation_errors"] = evidence_errors
    report["proven_checks"] = [check for check in checks if check.get("status") == PROVEN_STATUS]
    report["assumptions"] = []
    report["passed"] = bool(
        not evidence_errors
        and all(bool(check.get("passed", False)) for check in checks)
        and all(check.get("status") == PROVEN_STATUS for check in checks)
    )
    report["status"] = "PASS" if report["passed"] else "FAIL"
    return report


def build_host_gate_report(
    config_path: str,
    evidence_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    required_apps: Sequence[str] = DEFAULT_MEETING_APPS,
) -> Dict[str, Any]:
    config = load_effective_config(config_path)
    evidence = load_host_gate_evidence(evidence_path) if evidence_path is not None else None
    return evaluate_host_gate(config, evidence, dry_run=dry_run, required_apps=required_apps)


def _evaluate_cold_boot(cold_boot: Dict[str, Any]) -> Dict[str, Any]:
    host_visible = bool(cold_boot.get("host_visible_microphone", False))
    boot_time_s = _as_float(cold_boot.get("boot_time_s"))
    evidence = {
        "host_visible_microphone": host_visible,
        "boot_time_s": boot_time_s,
        "artifact_path": cold_boot.get("artifact_path"),
        "device_name": cold_boot.get("device_name"),
    }
    passed = host_visible and boot_time_s is not None
    failure = None if passed else "cold boot evidence must prove a host-visible microphone and include boot_time_s"
    status = PROVEN_STATUS if passed else FAILED_STATUS
    return _make_check(
        "cold_boot_host_visible_mic",
        passed=passed,
        status=status,
        evidence=evidence,
        assumption="Cold boot to host-visible microphone is assumed until real hardware evidence is attached.",
        failure=failure,
    )


def _evaluate_reconnect(reconnect: Dict[str, Any]) -> Dict[str, Any]:
    recovered = bool(reconnect.get("recovered", False))
    reconnect_time_s = _as_float(reconnect.get("reconnect_time_s"))
    evidence = {
        "recovered": recovered,
        "reconnect_time_s": reconnect_time_s,
        "artifact_path": reconnect.get("artifact_path"),
    }
    passed = recovered and reconnect_time_s is not None
    failure = None if passed else "reconnect evidence must prove recovery and include reconnect_time_s"
    status = PROVEN_STATUS if passed else FAILED_STATUS
    return _make_check(
        "reconnect_recovery",
        passed=passed,
        status=status,
        evidence=evidence,
        assumption="Unplug/replug recovery is assumed until a hardware evidence bundle proves it.",
        failure=failure,
    )


def _evaluate_meeting_app_verdicts(
    meeting_apps: List[Dict[str, Any]],
    required_apps: Sequence[str],
) -> tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    app_results: List[Dict[str, Any]] = []
    seen_required: set[str] = set()
    for index, item in enumerate(meeting_apps):
        if not isinstance(item, dict):
            errors.append(f"meeting_apps[{index}] must be an object")
            continue
        missing = [key for key in REQUIRED_MEETING_APP_KEYS if key not in item]
        if missing:
            errors.append(f"meeting_apps[{index}] missing keys: {', '.join(missing)}")
            continue
        verdict = str(item.get("verdict", "")).strip().lower()
        app_name = str(item.get("app", "")).strip()
        if verdict not in {"pass", "fail"}:
            errors.append(f"meeting_apps[{index}].verdict must be 'pass' or 'fail'")
        continuous_capture_s = _as_float(item.get("duration_s"))
        artifact_path = str(item.get("artifact_path", "")).strip()
        selected_input_device = str(item.get("selected_input_device", "")).strip()
        app_passed = verdict == "pass" and continuous_capture_s is not None and bool(artifact_path) and bool(selected_input_device)
        app_results.append(
            {
                "app": app_name,
                "verdict": verdict or "unknown",
                "artifact_path": artifact_path,
                "selected_input_device": selected_input_device,
                "duration_s": continuous_capture_s,
                "passed": app_passed,
            }
        )
        normalized = _normalize_app_name(app_name)
        if normalized:
            seen_required.add(normalized)

    required_normalized = {_normalize_app_name(app) for app in required_apps}
    missing_required = sorted(name for name in required_normalized if name not in seen_required)
    if missing_required:
        errors.append(f"missing meeting app evidence for: {', '.join(missing_required)}")

    passed = not errors and all(bool(item.get("passed", False)) for item in app_results)
    status = PROVEN_STATUS if passed else FAILED_STATUS
    evidence = {
        "required_apps": list(required_apps),
        "received_apps": app_results,
        "required_count": len(required_apps),
    }
    return (
        _make_check(
            "meeting_app_verdict_artifacts",
            passed=passed,
            status=status,
            evidence=evidence,
            assumption="Meeting-app verdict artifacts are assumed until a hardware-backed evidence bundle is attached.",
            failure=None if passed else "meeting app evidence is missing, malformed, or incomplete",
        ),
        errors,
    )


def _assumed_check(name: str, assumption: str) -> Dict[str, Any]:
    return _make_check(name, passed=False, status=ASSUMED_STATUS, evidence={}, assumption=assumption)


def _failed_check(name: str, evidence_text: str, failure: str) -> Dict[str, Any]:
    return _make_check(name, passed=False, status=FAILED_STATUS, evidence={"detail": evidence_text}, failure=failure)


def _make_check(
    name: str,
    *,
    passed: bool,
    status: str,
    evidence: Dict[str, Any],
    assumption: Optional[str] = None,
    failure: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "status": status,
        "evidence": evidence,
        "assumption": assumption,
        "failure": failure,
    }


def _normalize_app_name(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    if "zoom" in lowered:
        return "zoom"
    if "meet" in lowered:
        return "meet"
    if "teams" in lowered:
        return "teams"
    return lowered


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _format_report(report: Dict[str, Any]) -> str:
    lines = [
        f"mode={report.get('mode', 'unknown')}",
        f"status={report.get('status', 'UNKNOWN')}",
        f"passed={bool(report.get('passed', False))}",
    ]
    if report.get("dry_run"):
        lines.append("dry_run=true")
    if report.get("validation_errors"):
        lines.append("validation_errors=" + "; ".join(str(item) for item in report["validation_errors"]))
    for check in report.get("checks", []):
        if not isinstance(check, dict):
            continue
        lines.append(
            f"{check.get('name')}: {check.get('status')} "
            f"passed={bool(check.get('passed', False))}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate host-facing meeting readiness.")
    parser.add_argument("--config", required=True, help="Effective FocusField config path.")
    parser.add_argument("--evidence-json", default="", help="Host evidence JSON bundle path.")
    parser.add_argument("--dry-run", action="store_true", help="Emit assumptions only; do not claim hardware proof.")
    parser.add_argument("--json", action="store_true", help="Print the full verdict as JSON.")
    parser.add_argument(
        "--required-app",
        action="append",
        default=[],
        help="Optional meeting app requirement override. Can be passed multiple times.",
    )
    args = parser.parse_args(argv)

    config = load_effective_config(args.config)
    evidence = load_host_gate_evidence(args.evidence_json) if args.evidence_json else None
    required_apps = tuple(args.required_app) if args.required_app else DEFAULT_MEETING_APPS
    report = evaluate_host_gate(config, evidence, dry_run=bool(args.dry_run), required_apps=required_apps)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report))
    if report.get("status") == DRY_RUN_STATUS:
        return 0
    return 0 if bool(report.get("passed", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
