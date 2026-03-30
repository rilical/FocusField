"""Helpers for loading shared benchmark and perf-gate profiles.

The release-entrypoint scripts use this module to resolve their default
profile path and to load threshold overrides from YAML profiles without
failing when a profile file is absent in a clean checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml


_DEFAULT_PROFILE_CANDIDATES = (
    "configs/full_3cam_8mic_pi_prod.yaml",
    "configs/full_3cam_8mic_pi.yaml",
    "configs/thresholds_presets.yaml",
)

_FOCUSBENCH_DEFAULTS: Dict[str, float] = {
    "si_sdr_delta_db_min": 2.0,
    "stoi_delta_min": 0.03,
    "wer_relative_improvement_min": 0.12,
    "sir_delta_db_min": 4.0,
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

_PI_PERF_GATE_DEFAULTS: Dict[str, float] = {
    "latency_p95_max": 150.0,
    "latency_p99_max": 220.0,
    "overflow_delta_max": 4.0,
    "queue_full_max": 25.0,
    "no_candidates_ratio_max": 0.25,
    "speech_with_no_lock_ratio_max": 0.20,
    "no_faces_fallback_ratio_max": 0.20,
    "overflow_rate_max_per_min": 4.0,
    "face_track_rate_min": 1.0,
    "face_detection_stall_max_ms": 2000.0,
    "lock_continuity_ratio_min": 0.70,
    "min_runtime_seconds": 60.0,
    "no_candidates_denominator_min": 5.0,
}


def default_pi_nightly_profile_path() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    for candidate in _DEFAULT_PROFILE_CANDIDATES:
        path = (repo_root / candidate).resolve()
        if path.exists():
            return path
    return (repo_root / _DEFAULT_PROFILE_CANDIDATES[0]).resolve()


def load_focusbench_thresholds(profile_path: str | Path | None) -> Dict[str, float]:
    doc = _load_profile_document(profile_path)
    overrides: Dict[str, float] = {}
    _merge_numeric_mapping(overrides, _extract_mapping(doc, ("bench", "targets")))
    _merge_numeric_mapping(overrides, _extract_mapping(doc, ("focusbench",)))
    _merge_numeric_mapping(overrides, _extract_mapping(doc, ("bench", "thresholds")))
    return overrides


def load_pi_perf_gate_thresholds(profile_path: str | Path | None) -> Dict[str, float]:
    thresholds = dict(_PI_PERF_GATE_DEFAULTS)
    doc = _load_profile_document(profile_path)
    _merge_numeric_mapping(thresholds, _extract_mapping(doc, ("pi_perf_gate",)))
    _merge_numeric_mapping(thresholds, _extract_mapping(doc, ("perf_gate",)))
    _merge_numeric_mapping(thresholds, _extract_mapping(doc, ("bench", "targets")))
    _merge_numeric_mapping(thresholds, _extract_mapping(doc, ("bench", "perf_gate")))
    _merge_numeric_mapping(thresholds, _extract_mapping(doc, ("thresholds",)))
    return thresholds


def _load_profile_document(profile_path: str | Path | None) -> Dict[str, Any]:
    if profile_path is None:
        return {}
    path = Path(profile_path).expanduser()
    if not path.exists() or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_mapping(doc: Mapping[str, Any], path: Iterable[str]) -> Dict[str, Any]:
    current: Any = doc
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _merge_numeric_mapping(target: Dict[str, float], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        try:
            target[str(key)] = float(value)
        except Exception:
            continue
