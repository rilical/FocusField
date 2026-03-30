#!/usr/bin/env python3
"""Build a demo-readiness verdict from host evidence and runtime artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.bench.metrics.metrics import compute_drop_stats, compute_latency_stats, compute_runtime_summary
from focusfield.bench.metrics.scoring import normalize_thresholds
from focusfield.core.config import load_config
from scripts.meeting_host_gate import build_host_gate_report
from scripts.support_bundle import resolve_run_dir


DEFAULT_REQUIRED_APPS = ("Zoom",)
DEFAULT_MIN_SOAK_SECONDS = 1800.0


def build_demo_readiness(
    config_path: str,
    *,
    run_dir: str = "artifacts/LATEST",
    host_gate_evidence: Optional[str] = None,
    min_soak_seconds: float = DEFAULT_MIN_SOAK_SECONDS,
    required_apps: Sequence[str] = DEFAULT_REQUIRED_APPS,
) -> Dict[str, Any]:
    cfg = load_config(config_path)
    run_path = resolve_run_dir(run_dir)
    thresholds = normalize_thresholds(_bench_targets(cfg))
    host_report = build_host_gate_report(
        config_path,
        host_gate_evidence,
        required_apps=tuple(required_apps) if required_apps else DEFAULT_REQUIRED_APPS,
    )

    perf_path = run_path / "logs" / "perf.jsonl"
    events_path = run_path / "logs" / "events.jsonl"
    latency_summary = compute_latency_stats(perf_path) if perf_path.exists() else {}
    drop_summary = compute_drop_stats(events_path, perf_path) if perf_path.exists() and events_path.exists() else {}
    runtime_summary = compute_runtime_summary(perf_path) if perf_path.exists() else {}
    soak_summary = _compute_soak_summary(run_path, min_soak_seconds=min_soak_seconds)

    boot_time_s = _host_check_value(host_report, "cold_boot_host_visible_mic", "boot_time_s")
    reconnect_time_s = _host_check_value(host_report, "reconnect_recovery", "reconnect_time_s")
    zoom_input = _zoom_selected_input_device(host_report)

    checks = [
        _check_bool(
            "host_meeting_gate",
            bool(host_report.get("passed", False)),
            expected="Zoom host evidence proves cold boot, reconnect, and device selection.",
            actual=str(host_report.get("status", "FAIL")),
        ),
        _check_max(
            "latency_p95_ms",
            _as_optional_float(latency_summary.get("p95_ms")),
            float(thresholds["latency_p95_ms_max"]),
            "ms",
        ),
        _check_max(
            "latency_p99_ms",
            _as_optional_float(latency_summary.get("p99_ms")),
            float(thresholds["latency_p99_ms_max"]),
            "ms",
        ),
        _check_max(
            "output_underrun_rate",
            _as_optional_float(runtime_summary.get("output_underrun_rate")),
            float(thresholds["output_underrun_rate_max"]),
            "",
        ),
        _check_max(
            "queue_pressure_peak",
            _as_optional_float(runtime_summary.get("queue_pressure_peak")),
            float(thresholds["queue_pressure_max"]),
            "",
        ),
        _check_bool(
            "crash_free_soak",
            bool(soak_summary.get("passed", False)),
            expected=f">= {float(min_soak_seconds):.1f}s without crash artifacts",
            actual=_format_soak_actual(soak_summary),
        ),
    ]

    passed = all(bool(item.get("passed", False)) for item in checks)
    payload = {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config_path": str(Path(config_path).expanduser().resolve()),
        "run_dir": str(run_path),
        "required_apps": list(required_apps),
        "passed": passed,
        "status": "PASS" if passed else "FAIL",
        "thresholds": {
            "latency_p95_ms_max": float(thresholds["latency_p95_ms_max"]),
            "latency_p99_ms_max": float(thresholds["latency_p99_ms_max"]),
            "output_underrun_rate_max": float(thresholds["output_underrun_rate_max"]),
            "queue_pressure_max": float(thresholds["queue_pressure_max"]),
            "min_soak_seconds": float(min_soak_seconds),
        },
        "summary": {
            "boot_to_host_visible_mic_s": boot_time_s,
            "reconnect_time_s": reconnect_time_s,
            "zoom_selected_input_device": zoom_input,
            "latency": latency_summary,
            "drops": drop_summary,
            "runtime": runtime_summary,
            "soak": soak_summary,
        },
        "checks": checks,
        "artifacts": {
            "host_gate_evidence": str(Path(host_gate_evidence).expanduser().resolve()) if host_gate_evidence else "",
            "perf_log": str(perf_path),
            "events_log": str(events_path),
            "crash_dir": str(run_path / "crash"),
        },
        "host_gate": host_report,
    }
    return payload


def write_demo_readiness(payload: Dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a FocusField demo readiness verdict")
    parser.add_argument("--config", required=True, help="Active demo config path")
    parser.add_argument("--run-dir", default="artifacts/LATEST", help="Run directory or artifacts/LATEST pointer")
    parser.add_argument("--host-gate-evidence", default="", help="meeting_host_gate evidence bundle")
    parser.add_argument("--output", default="demo_readiness.json", help="JSON report path")
    parser.add_argument("--min-soak-seconds", type=float, default=DEFAULT_MIN_SOAK_SECONDS, help="Minimum crash-free rehearsal duration")
    args = parser.parse_args(argv)

    payload = build_demo_readiness(
        args.config,
        run_dir=args.run_dir,
        host_gate_evidence=args.host_gate_evidence or None,
        min_soak_seconds=max(60.0, float(args.min_soak_seconds)),
    )
    output = write_demo_readiness(payload, args.output)
    print(output)
    return 0 if bool(payload.get("passed", False)) else 2


def _bench_targets(config: Dict[str, Any]) -> Dict[str, Any]:
    bench_cfg = config.get("bench", {})
    if not isinstance(bench_cfg, dict):
        return {}
    targets = bench_cfg.get("targets", {})
    return targets if isinstance(targets, dict) else {}


def _compute_soak_summary(run_path: Path, *, min_soak_seconds: float) -> Dict[str, Any]:
    perf_path = run_path / "logs" / "perf.jsonl"
    crash_dir = run_path / "crash"
    first_t_ns: Optional[int] = None
    last_t_ns: Optional[int] = None
    if perf_path.exists():
        for row in _read_jsonl(perf_path):
            t_ns = _as_optional_int(row.get("t_ns"))
            if t_ns is None:
                continue
            if first_t_ns is None:
                first_t_ns = t_ns
            last_t_ns = t_ns
    duration_s = 0.0
    if first_t_ns is not None and last_t_ns is not None and last_t_ns > first_t_ns:
        duration_s = float(last_t_ns - first_t_ns) / 1_000_000_000.0
    crash_artifacts = []
    if crash_dir.exists():
        crash_artifacts = sorted(str(item.resolve()) for item in crash_dir.iterdir() if item.is_file())
    passed = duration_s >= float(min_soak_seconds) and not crash_artifacts
    return {
        "passed": passed,
        "duration_s": duration_s,
        "min_required_s": float(min_soak_seconds),
        "crash_artifact_count": len(crash_artifacts),
        "crash_artifacts": crash_artifacts,
    }


def _host_check_value(report: Dict[str, Any], name: str, field: str) -> Optional[float]:
    for check in report.get("checks", []):
        if not isinstance(check, dict) or str(check.get("name", "")) != name:
            continue
        evidence = check.get("evidence")
        if not isinstance(evidence, dict):
            return None
        return _as_optional_float(evidence.get(field))
    return None


def _zoom_selected_input_device(report: Dict[str, Any]) -> str:
    for check in report.get("checks", []):
        if not isinstance(check, dict) or str(check.get("name", "")) != "meeting_app_verdict_artifacts":
            continue
        evidence = check.get("evidence")
        if not isinstance(evidence, dict):
            continue
        for item in evidence.get("received_apps", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("app", "")).strip().lower() != "zoom":
                continue
            return str(item.get("selected_input_device", "") or "")
    return ""


def _check_bool(name: str, passed: bool, *, expected: str, actual: str) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def _check_max(name: str, actual: Optional[float], maximum: float, unit: str) -> Dict[str, Any]:
    expected = f"<= {maximum:.4f}{(' ' + unit) if unit else ''}".rstrip()
    if actual is None:
        return {"name": name, "passed": False, "expected": expected, "actual": "missing"}
    actual_text = f"{actual:.4f}{(' ' + unit) if unit else ''}".rstrip()
    return {"name": name, "passed": bool(actual <= maximum), "expected": expected, "actual": actual_text}


def _format_soak_actual(soak_summary: Dict[str, Any]) -> str:
    duration_s = _as_optional_float(soak_summary.get("duration_s")) or 0.0
    crash_count = int(soak_summary.get("crash_artifact_count", 0) or 0)
    return f"{duration_s:.1f}s, crash_artifacts={crash_count}"


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
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
                rows.append(payload)
    return rows


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
