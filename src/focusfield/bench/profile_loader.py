"""Shared benchmark profile loader for Pi gates and A/B scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from focusfield.bench.metrics.scoring import default_thresholds as focusbench_default_thresholds


def default_pi_nightly_profile_path() -> Path:
    return Path(__file__).resolve().parents[3] / "configs" / "bench_profiles" / "pi_realtime_nightly.yaml"


def load_bench_profile(profile_path: Optional[str]) -> Dict[str, Any]:
    if profile_path:
        path = Path(profile_path).expanduser().resolve()
    else:
        path = default_pi_nightly_profile_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_pi_perf_gate_thresholds(profile_path: Optional[str]) -> Dict[str, float]:
    defaults: Dict[str, float] = {
        "latency_p95_max": 350.0,
        "latency_p99_max": 550.0,
        "overflow_delta_max": 5.0,
        "queue_full_max": 5.0,
        "no_candidates_ratio_max": 0.65,
        "speech_with_no_lock_ratio_max": 0.55,
        "no_faces_fallback_ratio_max": 0.75,
        "overflow_rate_max_per_min": 8.0,
        "face_track_rate_min": 0.6,
        "face_detection_stall_max_ms": 1800.0,
        "lock_continuity_ratio_min": 0.45,
        "min_runtime_seconds": 120.0,
        "no_candidates_denominator_min": 10.0,
    }
    profile = load_bench_profile(profile_path)
    section = profile.get("pi_perf_gate", {}) if isinstance(profile, dict) else {}
    if not isinstance(section, dict):
        return defaults
    merged = dict(defaults)
    for key in list(merged.keys()):
        if key not in section:
            continue
        try:
            merged[key] = float(section[key])
        except Exception:
            continue
    return merged


def load_focusbench_thresholds(profile_path: Optional[str]) -> Dict[str, float]:
    defaults = dict(focusbench_default_thresholds())
    profile = load_bench_profile(profile_path)
    section = profile.get("focusbench", {}) if isinstance(profile, dict) else {}
    if not isinstance(section, dict):
        return defaults
    thresholds = section.get("thresholds", {})
    if not isinstance(thresholds, dict):
        return defaults
    merged = dict(defaults)
    for key in list(merged.keys()):
        if key not in thresholds:
            continue
        try:
            merged[key] = float(thresholds[key])
        except Exception:
            continue
    return merged
