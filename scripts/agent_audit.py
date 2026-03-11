#!/usr/bin/env python3
"""Agent-first audit runner for FocusField Pi reliability and benchmark gates."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from focusfield.bench.profile_loader import default_pi_nightly_profile_path


def _resolve_run_dir(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_file():
        ref = path.read_text(encoding="utf-8").strip()
        if ref:
            return (path.parent / ref).resolve()
    return path.resolve()


def _run_command(cmd: List[str], cwd: Path) -> Dict[str, Any]:
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    elapsed = time.time() - started
    return {
        "cmd": cmd,
        "exit_code": int(proc.returncode),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_s": float(elapsed),
    }


def _add_finding(findings: List[Dict[str, Any]], severity: str, title: str, detail: str, step: str) -> None:
    findings.append(
        {
            "severity": severity,
            "title": title,
            "detail": detail,
            "step": step,
        }
    )


def _write_markdown(
    out_path: Path,
    verdict: str,
    findings: List[Dict[str, Any]],
    steps: Dict[str, Dict[str, Any]],
    run_dir: Path,
    config_path: str,
    profile_path: str,
) -> None:
    lines: List[str] = []
    lines.append("# FocusField Audit Report")
    lines.append("")
    lines.append(f"- `verdict`: **{verdict}**")
    lines.append(f"- `run_dir`: `{run_dir}`")
    lines.append(f"- `config`: `{config_path}`")
    lines.append(f"- `profile`: `{profile_path}`")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if findings:
        for item in findings:
            lines.append(
                f"- [{item.get('severity','P2')}] {item.get('title','Finding')} "
                f"(step: `{item.get('step','unknown')}`): {item.get('detail','')}"
            )
    else:
        lines.append("- No findings.")
    lines.append("")
    lines.append("## Step Results")
    lines.append("")
    for step_name, result in steps.items():
        lines.append(
            f"- `{step_name}`: exit={result.get('exit_code')} duration={result.get('duration_s', 0.0):.2f}s"
        )
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FocusField agent-first audit and emit JSON/Markdown reports.")
    parser.add_argument("--config", default="configs/full_3cam_8mic_pi_prod.yaml")
    parser.add_argument("--run-dir", default="artifacts/LATEST")
    parser.add_argument("--profile", default=str(default_pi_nightly_profile_path()))
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--camera-source", default="by-path")
    parser.add_argument("--camera-scope", default="usb")
    parser.add_argument("--require-cameras", type=int, default=3)
    parser.add_argument("--require-audio-channels", type=int, default=8)
    parser.add_argument("--strict", dest="strict", action="store_true", default=True)
    parser.add_argument("--no-strict", dest="strict", action="store_false")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--full-pytest", action="store_true")
    parser.add_argument("--min-runtime-seconds", type=float, default=None)
    parser.add_argument("--baseline-run", default="")
    parser.add_argument("--candidate-run", default="")
    parser.add_argument("--scene-manifest", default="")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    run_dir = _resolve_run_dir(args.run_dir)
    audit_dir = run_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    steps: Dict[str, Dict[str, Any]] = {}
    findings: List[Dict[str, Any]] = []

    preflight_json = audit_dir / "preflight.json"
    preflight_cmd = [
        args.python_bin,
        "scripts/pi_preflight.py",
        "--config",
        args.config,
        "--camera-source",
        args.camera_source,
        "--camera-scope",
        args.camera_scope,
        "--require-cameras",
        str(args.require_cameras),
        "--require-audio-channels",
        str(args.require_audio_channels),
        "--json-out",
        str(preflight_json),
    ]
    if args.strict:
        preflight_cmd.append("--strict")
    steps["preflight"] = _run_command(preflight_cmd, repo_root)
    if steps["preflight"]["exit_code"] != 0:
        _add_finding(
            findings,
            "P0",
            "Preflight contract failed",
            "Preflight did not pass strict camera/audio/LED checks.",
            "preflight",
        )

    if not args.skip_tests:
        subset_cmd = [
            args.python_bin,
            "-m",
            "pytest",
            "-q",
            "tests/test_lock_and_fallback.py",
            "tests/test_pi_contract.py",
            "tests/test_telemetry_truth.py",
            "tests/test_uma8_leds.py",
            "tests/test_focusbench_metrics.py",
        ]
        steps["tests_subset"] = _run_command(subset_cmd, repo_root)
        if steps["tests_subset"]["exit_code"] != 0:
            _add_finding(
                findings,
                "P0",
                "Unit contract regression",
                "Core fusion/pi contract test subset failed.",
                "tests_subset",
            )

        if args.full_pytest:
            full_cmd = [args.python_bin, "-m", "pytest", "-q"]
            steps["tests_full"] = _run_command(full_cmd, repo_root)
            if steps["tests_full"]["exit_code"] != 0:
                _add_finding(
                    findings,
                    "P1",
                    "Full test suite failure",
                    "Full pytest run reported failures.",
                    "tests_full",
                )

    perf_cmd = [
        args.python_bin,
        "scripts/pi_perf_gate.py",
        "--run-dir",
        str(run_dir),
        "--profile",
        args.profile,
    ]
    if args.min_runtime_seconds is not None:
        perf_cmd.extend(["--min-runtime-seconds", str(args.min_runtime_seconds)])
    steps["perf_gate"] = _run_command(perf_cmd, repo_root)
    perf_rc = int(steps["perf_gate"]["exit_code"])
    if perf_rc == 1:
        _add_finding(
            findings,
            "P0",
            "Nightly performance gate failed",
            "Latency/overflow/continuity thresholds failed.",
            "perf_gate",
        )
    elif perf_rc == 3:
        _add_finding(
            findings,
            "P1",
            "Insufficient benchmark data",
            "Run duration or metric denominator was insufficient for scoring.",
            "perf_gate",
        )
    elif perf_rc != 0:
        _add_finding(
            findings,
            "P1",
            "Perf gate execution error",
            f"pi_perf_gate exited with code {perf_rc}.",
            "perf_gate",
        )

    if args.baseline_run and args.candidate_run and args.scene_manifest:
        focusbench_cmd = [
            args.python_bin,
            "scripts/focusbench_ab.py",
            "--baseline-run",
            args.baseline_run,
            "--candidate-run",
            args.candidate_run,
            "--scene-manifest",
            args.scene_manifest,
            "--output-dir",
            str(audit_dir / "focusbench"),
            "--config",
            args.config,
            "--profile",
            args.profile,
        ]
        steps["focusbench_ab"] = _run_command(focusbench_cmd, repo_root)
        if int(steps["focusbench_ab"]["exit_code"]) != 0:
            _add_finding(
                findings,
                "P1",
                "FocusBench A/B gate failed",
                "Benchmark A/B gate did not pass.",
                "focusbench_ab",
            )

    critical_fail = any(item.get("severity") == "P0" for item in findings)
    insufficient_only = (not critical_fail) and any(item.get("title") == "Insufficient benchmark data" for item in findings)
    if critical_fail:
        verdict = "FAIL"
    elif insufficient_only:
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "PASS"

    report_json = {
        "verdict": verdict,
        "run_dir": str(run_dir),
        "config": args.config,
        "profile": args.profile,
        "steps": steps,
        "findings": findings,
    }

    json_path = audit_dir / "AuditReport.json"
    md_path = audit_dir / "AuditReport.md"
    json_path.write_text(json.dumps(report_json, indent=2, sort_keys=True), encoding="utf-8")
    _write_markdown(
        md_path,
        verdict=verdict,
        findings=findings,
        steps=steps,
        run_dir=run_dir,
        config_path=args.config,
        profile_path=args.profile,
    )

    print(f"AuditReport JSON: {json_path}")
    print(f"AuditReport MD: {md_path}")
    print(f"VERDICT={verdict}")
    if verdict == "PASS":
        return 0
    if verdict == "INSUFFICIENT_DATA":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
