"""
CONTRACT: inline (source: src/focusfield/bench/scenes/manifest.md)
ROLE: Bench scene format definition.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bench.scenes.manifest_path: manifest file

PERF / TIMING:
  - n/a

FAILURE MODES:
  - invalid manifest -> log manifest_invalid

LOG EVENTS:
  - module=bench.scenes.manifest, event=manifest_invalid, payload keys=path, error

TESTS:
  - tests/test_bench_scenes.py must cover loading and validation

CONTRACT DETAILS (inline from src/focusfield/bench/scenes/manifest.md):
# Bench scene manifest

## Scene object

- scene_id and description.
- audio sources:
  - file paths
  - target angle(s)
  - interferer angle(s)
  - level ratios (SNR/SIR)
- optional video:
  - recorded frames or synthetic face-bearing labels
- ground truth:
  - true target angle over time
  - true speaker timeline
- outputs:
  - required metrics to compute for this scene
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from focusfield.bench.scenes.labels import (
    normalize_bearing_segments,
    normalize_speaker_segments,
    validate_bearing_segments,
    validate_speaker_segments,
)

REQUIRED_RELEASE_SCENE_FIELDS = (
    "scene_id",
    "audio_path",
    "video_paths",
    "reference_audio_path",
    "speaker_segments",
    "bearing_segments",
    "tags",
)


def load_scene_manifest(
    manifest_path: str | Path,
    *,
    require_release_fields: bool = True,
) -> Dict[str, Any]:
    path = Path(manifest_path).expanduser()
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    errors = validate_scene_manifest(raw, require_release_fields=require_release_fields, source=str(path))
    if errors:
        raise ValueError("; ".join(errors))
    return normalize_scene_manifest(raw, base_dir=path.parent, source_path=str(path))


def validate_scene_manifest(
    raw_manifest: Any,
    *,
    require_release_fields: bool = True,
    source: Optional[str] = None,
) -> List[str]:
    errors: List[str] = []
    scenes = _extract_scenes(raw_manifest, errors, source=source)
    if not scenes:
        errors.append(_format_error(source, "manifest must contain at least one scene"))
        return errors

    for index, scene in enumerate(scenes):
        prefix = f"scene[{index}]"
        if not isinstance(scene, dict):
            errors.append(_format_error(source, f"{prefix} must be a mapping"))
            continue

        scene_id = scene.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            errors.append(_format_error(source, f"{prefix}.scene_id must be a non-empty string"))

        if require_release_fields:
            for field in REQUIRED_RELEASE_SCENE_FIELDS:
                if not _has_value(scene.get(field)):
                    errors.append(_format_error(source, f"{prefix}.{field} is required for release manifests"))

        video_paths = scene.get("video_paths")
        if _has_value(video_paths) and not isinstance(video_paths, list):
            errors.append(_format_error(source, f"{prefix}.video_paths must be a list of paths"))

        tags = scene.get("tags")
        if _has_value(tags):
            if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
                errors.append(_format_error(source, f"{prefix}.tags must be a list of non-empty strings"))

        speaker_errors = validate_speaker_segments(scene.get("speaker_segments"), scene_id=str(scene_id or prefix), source=source)
        bearing_errors = validate_bearing_segments(scene.get("bearing_segments"), scene_id=str(scene_id or prefix), source=source)
        errors.extend(speaker_errors)
        errors.extend(bearing_errors)

        metrics = scene.get("required_metrics")
        if _has_value(metrics) and (
            not isinstance(metrics, list) or not all(isinstance(item, str) and item.strip() for item in metrics)
        ):
            errors.append(_format_error(source, f"{prefix}.required_metrics must be a list of strings"))

    return errors


def normalize_scene_manifest(
    raw_manifest: Any,
    *,
    base_dir: Optional[Path] = None,
    source_path: Optional[str] = None,
) -> Dict[str, Any]:
    manifest: Dict[str, Any]
    if isinstance(raw_manifest, dict):
        manifest = copy.deepcopy(raw_manifest)
    elif isinstance(raw_manifest, list):
        manifest = {"scenes": copy.deepcopy(raw_manifest)}
    else:
        manifest = {"scenes": []}

    scenes = _extract_scenes(manifest, [], source=source_path)
    normalized_scenes = [
        _normalize_scene(scene, base_dir=base_dir, scene_index=index, source_path=source_path)
        for index, scene in enumerate(scenes)
        if isinstance(scene, dict)
    ]

    normalized: Dict[str, Any] = dict(manifest)
    normalized["scenes"] = normalized_scenes
    if source_path is not None:
        normalized["source_path"] = source_path
    if base_dir is not None:
        normalized["base_dir"] = str(base_dir.resolve())
    return normalized


def _normalize_scene(
    scene: Dict[str, Any],
    *,
    base_dir: Optional[Path],
    scene_index: int,
    source_path: Optional[str],
) -> Dict[str, Any]:
    normalized = copy.deepcopy(scene)
    scene_id = str(normalized.get("scene_id", f"scene_{scene_index}") or f"scene_{scene_index}").strip()
    normalized["scene_id"] = scene_id
    if "description" in normalized and normalized["description"] is not None:
        normalized["description"] = str(normalized["description"])
    normalized["audio_path"] = _resolve_path(normalized.get("audio_path"), base_dir)
    normalized["reference_audio_path"] = _resolve_path(normalized.get("reference_audio_path"), base_dir)
    normalized["noise_reference_audio_path"] = _resolve_path(normalized.get("noise_reference_audio_path"), base_dir)
    normalized["target_reference_wav"] = _resolve_path(normalized.get("target_reference_wav"), base_dir)
    normalized["interferer_reference_wav"] = _resolve_path(normalized.get("interferer_reference_wav"), base_dir)
    normalized["baseline_audio_path"] = _resolve_path(normalized.get("baseline_audio_path"), base_dir)
    normalized["candidate_audio_path"] = _resolve_path(normalized.get("candidate_audio_path"), base_dir)
    normalized["video_paths"] = _normalize_path_list(normalized.get("video_paths"), base_dir)
    normalized["tags"] = _normalize_string_list(normalized.get("tags"))
    normalized["required_metrics"] = _normalize_string_list(normalized.get("required_metrics"))
    normalized["start_s"] = _normalize_optional_nonnegative_float(normalized.get("start_s", normalized.get("clip_start_s")))
    normalized["end_s"] = _normalize_optional_nonnegative_float(normalized.get("end_s", normalized.get("clip_end_s")))
    clip_cfg = normalized.get("clip")
    if isinstance(clip_cfg, dict):
        clip_start = _normalize_optional_nonnegative_float(
            clip_cfg.get("start_s", clip_cfg.get("start_sec", clip_cfg.get("start")))
        )
        clip_end = _normalize_optional_nonnegative_float(
            clip_cfg.get("end_s", clip_cfg.get("end_sec", clip_cfg.get("end")))
        )
        if normalized["start_s"] is None:
            normalized["start_s"] = clip_start
        if normalized["end_s"] is None:
            normalized["end_s"] = clip_end
    normalized["speaker_segments"] = normalize_speaker_segments(
        normalized.get("speaker_segments"),
        scene_id=scene_id,
        source=source_path,
    )
    normalized["bearing_segments"] = normalize_bearing_segments(
        normalized.get("bearing_segments"),
        scene_id=scene_id,
        source=source_path,
    )
    if base_dir is not None:
        normalized["resolved_paths"] = {
            "audio_path": normalized["audio_path"],
            "reference_audio_path": normalized["reference_audio_path"],
            "noise_reference_audio_path": normalized["noise_reference_audio_path"],
            "target_reference_wav": normalized["target_reference_wav"],
            "interferer_reference_wav": normalized["interferer_reference_wav"],
            "baseline_audio_path": normalized["baseline_audio_path"],
            "candidate_audio_path": normalized["candidate_audio_path"],
            "video_paths": list(normalized["video_paths"]),
        }
    return normalized


def _extract_scenes(raw_manifest: Any, errors: List[str], *, source: Optional[str]) -> List[Dict[str, Any]]:
    if raw_manifest is None:
        errors.append(_format_error(source, "manifest is empty"))
        return []
    if isinstance(raw_manifest, list):
        return list(raw_manifest)
    if isinstance(raw_manifest, dict):
        scenes = raw_manifest.get("scenes")
        if isinstance(scenes, list):
            return list(scenes)
        errors.append(_format_error(source, "manifest.scenes must be a list"))
        return []
    errors.append(_format_error(source, "manifest must be a mapping or a list of scenes"))
    return []


def _normalize_path_list(value: Any, base_dir: Optional[Path]) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [p for p in (_resolve_path(item, base_dir) for item in items) if p]


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _resolve_path(value: Any, base_dir: Optional[Path]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if base_dir is not None and not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return str(path)


def _normalize_optional_nonnegative_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out < 0.0:
        return None
    return out


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _format_error(source: Optional[str], message: str) -> str:
    return f"{source}: {message}" if source else message
