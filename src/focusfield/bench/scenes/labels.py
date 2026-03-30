"""
CONTRACT: inline (source: src/focusfield/bench/scenes/labels.md)
ROLE: Ground-truth label format.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - n/a

PERF / TIMING:
  - n/a

FAILURE MODES:
  - invalid labels -> log not_implemented/label_invalid

LOG EVENTS:
  - module=<name>, event=label_invalid, payload keys=scene_id, error

TESTS:
  - tests/test_bench_scenes.py must cover speaker/bearing normalization

CONTRACT DETAILS (inline from src/focusfield/bench/scenes/labels.md):
# Label format

- Ground-truth speaker timeline format.
- Angle labels and time alignment rules.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


def normalize_speaker_segments(
    segments: Any,
    *,
    scene_id: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    normalized, errors = _normalize_segments(
        segments,
        scene_id=scene_id,
        source=source,
        kind="speaker",
    )
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def validate_speaker_segments(
    segments: Any,
    *,
    scene_id: str,
    source: Optional[str] = None,
) -> List[str]:
    _, errors = _normalize_segments(segments, scene_id=scene_id, source=source, kind="speaker")
    return errors


def normalize_bearing_segments(
    segments: Any,
    *,
    scene_id: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    normalized, errors = _normalize_segments(
        segments,
        scene_id=scene_id,
        source=source,
        kind="bearing",
    )
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def validate_bearing_segments(
    segments: Any,
    *,
    scene_id: str,
    source: Optional[str] = None,
) -> List[str]:
    _, errors = _normalize_segments(segments, scene_id=scene_id, source=source, kind="bearing")
    return errors


def _normalize_segments(
    segments: Any,
    *,
    scene_id: str,
    source: Optional[str],
    kind: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    if segments is None:
        return [], []
    if not isinstance(segments, list):
        return [], [_format_error(source, f"{scene_id}.{kind}_segments must be a list")]

    normalized: List[Dict[str, Any]] = []
    previous_start = None
    for index, segment in enumerate(segments):
        prefix = f"{scene_id}.{kind}_segments[{index}]"
        if not isinstance(segment, dict):
            errors.append(_format_error(source, f"{prefix} must be a mapping"))
            continue

        start_s = _coerce_time(segment, prefix, source, "start")
        end_s = _coerce_time(segment, prefix, source, "end")
        if start_s is None or end_s is None:
            continue
        if end_s <= start_s:
            errors.append(_format_error(source, f"{prefix} end must be greater than start"))
            continue
        if previous_start is not None and start_s < previous_start:
            errors.append(_format_error(source, f"{prefix} must be sorted by start time"))
            continue
        previous_start = start_s

        normalized_segment: Dict[str, Any] = {
            "start_s": start_s,
            "end_s": end_s,
        }
        if kind == "speaker":
            speaker_id = segment.get("speaker_id", segment.get("speaker"))
            if speaker_id is None or not str(speaker_id).strip():
                errors.append(_format_error(source, f"{prefix}.speaker_id is required"))
                continue
            normalized_segment["speaker_id"] = str(speaker_id).strip()
            if "confidence" in segment and segment["confidence"] is not None:
                confidence = _coerce_float(segment["confidence"])
                if confidence is None or not (0.0 <= confidence <= 1.0):
                    errors.append(_format_error(source, f"{prefix}.confidence must be between 0 and 1"))
                    continue
                normalized_segment["confidence"] = confidence
        else:
            angle = _coerce_float(segment.get("angle_deg"))
            if angle is None:
                errors.append(_format_error(source, f"{prefix}.angle_deg is required"))
                continue
            normalized_segment["angle_deg"] = _wrap_angle(angle)
            if "confidence" in segment and segment["confidence"] is not None:
                confidence = _coerce_float(segment["confidence"])
                if confidence is None or not (0.0 <= confidence <= 1.0):
                    errors.append(_format_error(source, f"{prefix}.confidence must be between 0 and 1"))
                    continue
                normalized_segment["confidence"] = confidence

        for key in ("label", "source_id", "camera_id"):
            if key in segment and segment[key] is not None:
                normalized_segment[key] = str(segment[key]).strip()
        normalized.append(normalized_segment)
    return normalized, errors


def _coerce_time(segment: Dict[str, Any], prefix: str, source: Optional[str], field: str) -> Optional[float]:
    candidates = (
        field,
        f"{field}_s",
        f"{field}_sec",
        f"{field}_seconds",
        f"{field}_time_s",
        f"{field}_ms",
    )
    for name in candidates:
        if name not in segment:
            continue
        value = _coerce_float(segment.get(name))
        if value is None:
            return None
        if name.endswith("_ms"):
            value /= 1000.0
        return float(value)
    return None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _wrap_angle(angle: float) -> float:
    return float(angle % 360.0)


def _format_error(source: Optional[str], message: str) -> str:
    return f"{source}: {message}" if source else message
