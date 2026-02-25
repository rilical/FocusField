"""Pass/fail scoring helpers for FocusBench."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    expected: str
    actual: str


def default_thresholds() -> Dict[str, float]:
    return {
        "si_sdr_delta_db_min": 2.0,
        "stoi_delta_min": 0.03,
        "wer_relative_improvement_min": 0.12,
        "sir_delta_db_min": 4.0,
        "latency_p95_ms_max": 150.0,
        "latency_p99_ms_max": 220.0,
        "audio_queue_full_max": 25.0,
        "audio_underrun_rate_max": 0.005,
    }


def normalize_thresholds(thresholds: Optional[Dict[str, Any]]) -> Dict[str, float]:
    out = dict(default_thresholds())
    if not isinstance(thresholds, dict):
        return out
    for key in list(out.keys()):
        if key not in thresholds:
            continue
        try:
            out[key] = float(thresholds[key])
        except Exception:
            continue
    return out


def evaluate_gates(
    quality_summary: Dict[str, Any],
    latency_summary: Dict[str, Any],
    drop_summary: Dict[str, Any],
    thresholds: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    thr = normalize_thresholds(thresholds)

    checks = [
        _check_min(
            "si_sdr_delta_db",
            _as_optional_float(quality_summary.get("median_si_sdr_delta_db")),
            thr["si_sdr_delta_db_min"],
            "dB",
        ),
        _check_min(
            "stoi_delta",
            _as_optional_float(quality_summary.get("median_stoi_delta")),
            thr["stoi_delta_min"],
            "",
        ),
        _check_min(
            "wer_relative_improvement",
            _as_optional_float(quality_summary.get("median_wer_relative_improvement")),
            thr["wer_relative_improvement_min"],
            "",
        ),
        _check_min(
            "sir_delta_db",
            _as_optional_float(quality_summary.get("median_sir_delta_db")),
            thr["sir_delta_db_min"],
            "dB",
        ),
        _check_max(
            "latency_p95_ms",
            _as_optional_float(latency_summary.get("p95_ms")),
            thr["latency_p95_ms_max"],
            "ms",
        ),
        _check_max(
            "latency_p99_ms",
            _as_optional_float(latency_summary.get("p99_ms")),
            thr["latency_p99_ms_max"],
            "ms",
        ),
        _check_max(
            "audio_queue_full",
            _as_optional_float(drop_summary.get("queue_full_audio")),
            thr["audio_queue_full_max"],
            "events",
        ),
        _check_max(
            "audio_underrun_rate",
            _as_optional_float(drop_summary.get("capture_underrun_rate")),
            thr["audio_underrun_rate_max"],
            "",
        ),
    ]

    passed = all(item.passed for item in checks)
    return {
        "passed": bool(passed),
        "thresholds": thr,
        "checks": [
            {
                "name": item.name,
                "passed": item.passed,
                "expected": item.expected,
                "actual": item.actual,
            }
            for item in checks
        ],
    }


def _check_min(name: str, actual: Optional[float], minimum: float, unit: str) -> GateCheck:
    expected = f">= {minimum:.4f}{(' ' + unit) if unit else ''}".rstrip()
    if actual is None:
        return GateCheck(name=name, passed=False, expected=expected, actual="missing")
    passed = bool(actual >= minimum)
    actual_text = f"{actual:.4f}{(' ' + unit) if unit else ''}".rstrip()
    return GateCheck(name=name, passed=passed, expected=expected, actual=actual_text)


def _check_max(name: str, actual: Optional[float], maximum: float, unit: str) -> GateCheck:
    expected = f"<= {maximum:.4f}{(' ' + unit) if unit else ''}".rstrip()
    if actual is None:
        return GateCheck(name=name, passed=False, expected=expected, actual="missing")
    passed = bool(actual <= maximum)
    actual_text = f"{actual:.4f}{(' ' + unit) if unit else ''}".rstrip()
    return GateCheck(name=name, passed=passed, expected=expected, actual=actual_text)


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None
