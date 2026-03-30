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
        "speaker_selection_accuracy_min": 0.8,
        "steering_mae_deg_max": 15.0,
        "steering_rmse_deg_max": 20.0,
        "face_reacquire_latency_p95_ms_max": 750.0,
        "id_churn_rate_max": 2.0,
        "latency_p95_ms_max": 150.0,
        "latency_p99_ms_max": 220.0,
        "audio_queue_full_max": 25.0,
        "audio_underrun_rate_max": 0.005,
        "lock_jitter_rms_max": 12.0,
        "handoff_latency_p95_ms_max": 900.0,
        "false_handoff_rate_max": 0.2,
        "no_lock_during_speech_ratio_max": 0.2,
        "output_underrun_rate_max": 0.02,
        "queue_pressure_max": 25.0,
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
    lock_summary: Dict[str, Any] | None = None,
    conversation_summary: Dict[str, Any] | None = None,
    runtime_summary: Dict[str, Any] | None = None,
    label_summary: Dict[str, Any] | None = None,
    strict_truth: bool = False,
) -> Dict[str, Any]:
    thr = normalize_thresholds(thresholds)
    lock_summary = lock_summary if isinstance(lock_summary, dict) else {}
    conversation_summary = conversation_summary if isinstance(conversation_summary, dict) else {}
    runtime_summary = runtime_summary if isinstance(runtime_summary, dict) else {}
    label_summary = label_summary if isinstance(label_summary, dict) else {}

    queue_pressure_peak = runtime_summary.get("queue_pressure_peak", drop_summary.get("queue_pressure_peak"))
    output_underrun_rate = runtime_summary.get("output_underrun_rate", drop_summary.get("output_underrun_rate"))
    label_supported_scene_count = _as_optional_float(label_summary.get("label_supported_scene_count"))
    selection_accuracy = _as_optional_float(label_summary.get("speaker_selection_accuracy"))
    steering_mae = _as_optional_float(label_summary.get("steering_mae_deg"))
    steering_rmse = _as_optional_float(label_summary.get("steering_rmse_deg"))
    face_reacquire_p95 = _as_optional_float(label_summary.get("face_reacquire_latency_p95_ms"))
    id_churn_rate = _as_optional_float(label_summary.get("id_churn_rate"))

    min_check = _check_min if strict_truth else _check_optional_min
    max_check = _check_max if strict_truth else _check_optional_max

    checks = [
        min_check(
            "si_sdr_delta_db",
            _as_optional_float(quality_summary.get("median_si_sdr_delta_db")),
            thr["si_sdr_delta_db_min"],
            "dB",
        ),
        min_check(
            "stoi_delta",
            _as_optional_float(quality_summary.get("median_stoi_delta")),
            thr["stoi_delta_min"],
            "",
        ),
        min_check(
            "wer_relative_improvement",
            _as_optional_float(quality_summary.get("median_wer_relative_improvement")),
            thr["wer_relative_improvement_min"],
            "",
        ),
        min_check(
            "sir_delta_db",
            _as_optional_float(quality_summary.get("median_sir_delta_db")),
            thr["sir_delta_db_min"],
            "dB",
        ),
        max_check(
            "latency_p95_ms",
            _as_optional_float(latency_summary.get("p95_ms")),
            thr["latency_p95_ms_max"],
            "ms",
        ),
        max_check(
            "latency_p99_ms",
            _as_optional_float(latency_summary.get("p99_ms")),
            thr["latency_p99_ms_max"],
            "ms",
        ),
        max_check(
            "audio_queue_full",
            _as_optional_float(drop_summary.get("queue_full_audio")),
            thr["audio_queue_full_max"],
            "events",
        ),
        max_check(
            "audio_underrun_rate",
            _as_optional_float(drop_summary.get("capture_underrun_rate")),
            thr["audio_underrun_rate_max"],
            "",
        ),
        max_check(
            "lock_jitter_rms",
            _as_optional_float(lock_summary.get("rms_step_deg", lock_summary.get("std_step_deg"))),
            thr["lock_jitter_rms_max"],
            "deg",
        ),
        max_check(
            "handoff_latency_p95_ms",
            _as_optional_float(conversation_summary.get("handoff_latency_p95_ms")),
            thr["handoff_latency_p95_ms_max"],
            "ms",
        ),
        max_check(
            "false_handoff_rate",
            _as_optional_float(conversation_summary.get("false_handoff_rate")),
            thr["false_handoff_rate_max"],
            "",
        ),
        max_check(
            "no_lock_during_speech_ratio",
            _as_optional_float(conversation_summary.get("no_lock_during_speech_ratio")),
            thr["no_lock_during_speech_ratio_max"],
            "",
        ),
        max_check(
            "output_underrun_rate",
            _as_optional_float(output_underrun_rate),
            thr["output_underrun_rate_max"],
            "",
        ),
        max_check(
            "queue_pressure",
            _as_optional_float(queue_pressure_peak),
            thr["queue_pressure_max"],
            "",
        ),
        min_check(
            "speaker_selection_accuracy",
            selection_accuracy,
            thr["speaker_selection_accuracy_min"],
            "",
        ),
        max_check(
            "steering_mae_deg",
            steering_mae,
            thr["steering_mae_deg_max"],
            "deg",
        ),
        max_check(
            "steering_rmse_deg",
            steering_rmse,
            thr["steering_rmse_deg_max"],
            "deg",
        ),
        max_check(
            "face_reacquire_latency_p95_ms",
            face_reacquire_p95,
            thr["face_reacquire_latency_p95_ms_max"],
            "ms",
        ),
        max_check(
            "id_churn_rate",
            id_churn_rate,
            thr["id_churn_rate_max"],
            "",
        ),
    ]

    if strict_truth:
        checks.append(
            _check_min(
                "label_supported_scene_count",
                _as_optional_float(label_supported_scene_count),
                1.0,
                "scenes",
            )
        )

    passed = all(item.passed for item in checks)
    return {
        "passed": bool(passed),
        "thresholds": thr,
        "strict_truth": bool(strict_truth),
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


def _check_optional_min(name: str, actual: Optional[float], minimum: float, unit: str) -> GateCheck:
    expected = f">= {minimum:.4f}{(' ' + unit) if unit else ''}".rstrip()
    if actual is None:
        return GateCheck(name=name, passed=True, expected=expected, actual="n/a")
    passed = bool(actual >= minimum)
    actual_text = f"{actual:.4f}{(' ' + unit) if unit else ''}".rstrip()
    return GateCheck(name=name, passed=passed, expected=expected, actual=actual_text)


def _check_optional_max(name: str, actual: Optional[float], maximum: float, unit: str) -> GateCheck:
    expected = f"<= {maximum:.4f}{(' ' + unit) if unit else ''}".rstrip()
    if actual is None:
        return GateCheck(name=name, passed=True, expected=expected, actual="n/a")
    passed = bool(actual <= maximum)
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
