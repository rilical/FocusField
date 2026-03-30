#!/usr/bin/env python3
"""Top-level production release gate for FocusField.

This entrypoint intentionally avoids the broken legacy wrappers and evaluates
the release verdict directly from the underlying library functions and artifacts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.bench.focusbench import run_focusbench
from focusfield.bench.metrics.metrics import (
    compute_conversation_metrics,
    compute_drop_stats,
    compute_latency_stats,
    compute_lock_jitter,
    compute_runtime_summary,
)
from focusfield.bench.metrics.scoring import evaluate_gates, normalize_thresholds
from focusfield.core.config import load_config
from scripts.boot_validation import boot_plan, load_effective_config, validate_local_model_assets


@dataclass(frozen=True)
class ReleaseGateResult:
    name: str
    passed: bool
    details: Dict[str, Any]


def run_release_gate(args: argparse.Namespace) -> Dict[str, Any]:
    checks: List[ReleaseGateResult] = []
    config = load_config(args.config)

    unit_check = _run_pytest_suite(args.pytest_targets)
    checks.append(unit_check)
    if not unit_check.passed:
        return _finalize(config, checks)

    boot_check = _run_boot_validation(args.config)
    checks.append(boot_check)
    if not boot_check.passed:
        return _finalize(config, checks)

    perf_check = _run_pi_perf_gate(
        run_dir=args.candidate_run,
        thresholds=_runtime_perf_thresholds(config),
    )
    checks.append(perf_check)
    if not perf_check.passed:
        return _finalize(config, checks)

    host_check = _run_host_gate(args.host_gate_evidence)
    checks.append(host_check)
    if not host_check.passed:
        return _finalize(config, checks)

    bench_check = _run_focusbench_gate(
        baseline_run=args.baseline_run,
        candidate_run=args.candidate_run,
        scene_manifest=args.scene_manifest,
        output_dir=args.output_dir,
        thresholds=_bench_thresholds(config),
        require_truth=not bool(args.allow_ad_hoc_quality),
    )
    checks.append(bench_check)

    return _finalize(config, checks)


def _run_pytest_suite(targets: List[str]) -> ReleaseGateResult:
    pytest_targets = list(targets) if targets else ["tests"]
    command = [sys.executable, "-m", "pytest", "-q", *pytest_targets]
    proc = subprocess.run(command, capture_output=True, text=True)
    passed = proc.returncode == 0
    return ReleaseGateResult(
        name="unit_and_integration_tests",
        passed=passed,
        details={
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        },
    )


def _run_boot_validation(config_path: str) -> ReleaseGateResult:
    config = load_effective_config(config_path)
    plan = boot_plan(config)
    errors = validate_local_model_assets(config, config_path)
    passed = len(errors) == 0
    return ReleaseGateResult(
        name="boot_validation",
        passed=passed,
        details={
            "config_path": config_path,
            "boot_plan": plan,
            "errors": errors,
        },
    )


def _run_pi_perf_gate(run_dir: str, thresholds: Dict[str, float]) -> ReleaseGateResult:
    run_path = Path(run_dir)
    perf_path = run_path / "logs" / "perf.jsonl"
    events_path = run_path / "logs" / "events.jsonl"
    lock_path = run_path / "traces" / "lock.jsonl"
    faces_path = run_path / "traces" / "faces.jsonl"

    missing = [str(path) for path in (perf_path, events_path) if not path.exists()]
    if missing:
        return ReleaseGateResult(
            name="pi_perf_gate",
            passed=False,
            details={"run_dir": str(run_path), "missing": missing},
        )

    latency_summary = compute_latency_stats(perf_path)
    drop_summary = compute_drop_stats(events_path, perf_path)
    runtime_summary = compute_runtime_summary(perf_path)
    lock_summary = compute_lock_jitter(lock_path) if lock_path.exists() else {}
    conversation_summary = (
        compute_conversation_metrics(lock_path, faces_path, events_path)
        if lock_path.exists() and faces_path.exists()
        else {}
    )

    gate = evaluate_gates(
        quality_summary={},
        latency_summary=latency_summary,
        drop_summary=drop_summary,
        thresholds=thresholds,
        lock_summary=lock_summary,
        conversation_summary=conversation_summary,
        runtime_summary=runtime_summary,
    )
    return ReleaseGateResult(
        name="pi_perf_gate",
        passed=bool(gate.get("passed", False)),
        details={
            "run_dir": str(run_path),
            "latency_summary": latency_summary,
            "drop_summary": drop_summary,
            "runtime_summary": runtime_summary,
            "lock_summary": lock_summary,
            "conversation_summary": conversation_summary,
            "gates": gate,
        },
    )


def _run_host_gate(host_gate_evidence: str) -> ReleaseGateResult:
    evidence_path = Path(host_gate_evidence)
    if not evidence_path.exists():
        return ReleaseGateResult(
            name="host_meeting_gate",
            passed=False,
            details={"host_gate_evidence": str(evidence_path), "error": "missing_evidence"},
        )

    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ReleaseGateResult(
            name="host_meeting_gate",
            passed=False,
            details={"host_gate_evidence": str(evidence_path), "error": f"invalid_json: {exc}"},
        )

    checks = payload.get("checks", [])
    failed_checks = [
        item
        for item in checks
        if isinstance(item, dict) and not bool(item.get("passed", False))
    ] if isinstance(checks, list) else []

    passed_flag = bool(payload.get("passed", False))
    host_visible = payload.get("host_visible_mic")
    if host_visible is not None:
        passed_flag = passed_flag and bool(host_visible)
    passed = passed_flag and not failed_checks
    return ReleaseGateResult(
        name="host_meeting_gate",
        passed=passed,
        details={
            "host_gate_evidence": str(evidence_path),
            "summary": payload,
            "failed_checks": failed_checks,
        },
    )


def _run_focusbench_gate(
    baseline_run: str,
    candidate_run: str,
    scene_manifest: str,
    output_dir: str,
    thresholds: Dict[str, Any],
    require_truth: bool,
) -> ReleaseGateResult:
    report = run_focusbench(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        scene_manifest=scene_manifest,
        output_dir=output_dir,
        thresholds=thresholds,
    )
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    gates = summary.get("gates", {}) if isinstance(summary, dict) else {}
    quality = summary.get("quality", {}) if isinstance(summary, dict) else {}
    missing_quality = _missing_quality_metrics(quality)
    passed = bool(gates.get("passed", False)) if isinstance(gates, dict) else False
    if require_truth and missing_quality:
        passed = False
    return ReleaseGateResult(
        name="focusbench",
        passed=passed,
        details={
            "output_path": report.get("output_path"),
            "gates": gates,
            "quality_summary": quality,
            "missing_quality_metrics": missing_quality,
        },
    )


def _missing_quality_metrics(quality_summary: Dict[str, Any]) -> List[str]:
    required = (
        "median_si_sdr_delta_db",
        "median_stoi_delta",
        "median_wer_relative_improvement",
        "median_sir_delta_db",
    )
    missing = []
    for key in required:
        if quality_summary.get(key) is None:
            missing.append(key)
    return missing


def _runtime_perf_thresholds(config: Dict[str, Any]) -> Dict[str, float]:
    bench_cfg = config.get("bench", {})
    if not isinstance(bench_cfg, dict):
        bench_cfg = {}
    targets = bench_cfg.get("targets", {})
    if not isinstance(targets, dict):
        targets = {}
    defaults = normalize_thresholds(targets)
    # Use the same runtime-health gate semantics as the bench gate, but keep
    # this path independent from the broken legacy wrappers.
    return defaults


def _bench_thresholds(config: Dict[str, Any]) -> Dict[str, float]:
    bench_cfg = config.get("bench", {})
    if not isinstance(bench_cfg, dict):
        bench_cfg = {}
    targets = bench_cfg.get("targets", {})
    if not isinstance(targets, dict):
        targets = {}
    return normalize_thresholds(targets)


def _finalize(config: Dict[str, Any], checks: List[ReleaseGateResult]) -> Dict[str, Any]:
    passed = all(check.passed for check in checks)
    return {
        "passed": passed,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "details": check.details,
            }
            for check in checks
        ],
        "config_mode": str(config.get("runtime", {}).get("mode", "") or ""),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FocusField production release gate")
    parser.add_argument("--config", default="configs/full_3cam_8mic_pi_prod.yaml", help="Production config path.")
    parser.add_argument("--baseline-run", required=True, help="Baseline run artifacts directory.")
    parser.add_argument("--candidate-run", required=True, help="Candidate run artifacts directory.")
    parser.add_argument("--scene-manifest", required=True, help="Recorded scene manifest YAML path.")
    parser.add_argument("--output-dir", required=True, help="Output directory for BenchReport and release artifacts.")
    parser.add_argument(
        "--host-gate-evidence",
        default="artifacts/LATEST/host_gate.json",
        help="JSON evidence file for host-visible microphone validation.",
    )
    parser.add_argument(
        "--pytest-targets",
        nargs="*",
        default=["tests"],
        help="Pytest targets for the unit/integration test step.",
    )
    parser.add_argument(
        "--allow-ad-hoc-quality",
        action="store_true",
        help="Allow missing reference-quality metrics in FocusBench (not recommended for production).",
    )
    args = parser.parse_args(argv)

    report = run_release_gate(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if bool(report.get("passed", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
