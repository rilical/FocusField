"""
CONTRACT: inline (source: src/focusfield/bench/scenes/dataset_catalog.md)
ROLE: Dataset catalog and licensing notes.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - n/a

PERF / TIMING:
  - n/a

FAILURE MODES:
  - invalid catalog -> log not_implemented/catalog_invalid

LOG EVENTS:
  - module=<name>, event=catalog_invalid, payload keys=path, error

TESTS:
  - tests/test_bench_scenes.py must cover catalog loading and validation

CONTRACT DETAILS (inline from src/focusfield/bench/scenes/dataset_catalog.md):
# Dataset catalog

- Sources of audio/video clips.
- Licensing notes and usage constraints.
- Dataset versioning for reproducibility.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def load_dataset_catalog(catalog_path: str | Path) -> Dict[str, Any]:
    path = Path(catalog_path).expanduser()
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    errors = validate_dataset_catalog(raw, source=str(path))
    if errors:
        raise ValueError("; ".join(errors))
    return normalize_dataset_catalog(raw, base_dir=path.parent, source_path=str(path))


def validate_dataset_catalog(raw_catalog: Any, *, source: Optional[str] = None) -> List[str]:
    errors: List[str] = []
    datasets = _extract_datasets(raw_catalog, errors, source=source)
    if not datasets:
        errors.append(_format_error(source, "catalog must contain at least one dataset"))
        return errors

    for index, dataset in enumerate(datasets):
        prefix = f"dataset[{index}]"
        if not isinstance(dataset, dict):
            errors.append(_format_error(source, f"{prefix} must be a mapping"))
            continue
        for field in ("dataset_id", "version", "license"):
            if not _has_value(dataset.get(field)):
                errors.append(_format_error(source, f"{prefix}.{field} is required"))
        for field in ("name", "usage_constraints"):
            if field in dataset and dataset[field] is not None and not str(dataset[field]).strip():
                errors.append(_format_error(source, f"{prefix}.{field} must not be empty"))
        for field in ("sources", "scene_manifests", "clip_paths"):
            if field in dataset and dataset[field] is not None and not isinstance(dataset[field], list):
                errors.append(_format_error(source, f"{prefix}.{field} must be a list"))
    return errors


def normalize_dataset_catalog(
    raw_catalog: Any,
    *,
    base_dir: Optional[Path] = None,
    source_path: Optional[str] = None,
) -> Dict[str, Any]:
    catalog: Dict[str, Any]
    if isinstance(raw_catalog, dict):
        catalog = copy.deepcopy(raw_catalog)
    elif isinstance(raw_catalog, list):
        catalog = {"datasets": copy.deepcopy(raw_catalog)}
    else:
        catalog = {"datasets": []}

    datasets = _extract_datasets(catalog, [], source=source_path)
    normalized_datasets = [
        _normalize_dataset(dataset, base_dir=base_dir, dataset_index=index)
        for index, dataset in enumerate(datasets)
        if isinstance(dataset, dict)
    ]

    normalized: Dict[str, Any] = dict(catalog)
    normalized["datasets"] = normalized_datasets
    normalized["datasets_by_id"] = {
        entry["dataset_id"]: entry for entry in normalized_datasets if isinstance(entry, dict) and entry.get("dataset_id")
    }
    if source_path is not None:
        normalized["source_path"] = source_path
    if base_dir is not None:
        normalized["base_dir"] = str(base_dir.resolve())
    return normalized


def _normalize_dataset(dataset: Dict[str, Any], *, base_dir: Optional[Path], dataset_index: int) -> Dict[str, Any]:
    normalized = copy.deepcopy(dataset)
    dataset_id = str(normalized.get("dataset_id", f"dataset_{dataset_index}") or f"dataset_{dataset_index}").strip()
    normalized["dataset_id"] = dataset_id
    for field in ("name", "version", "license", "usage_constraints", "license_url", "notes"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = str(normalized[field]).strip()
    normalized["scene_manifests"] = _normalize_path_list(normalized.get("scene_manifests"), base_dir)
    normalized["clip_paths"] = _normalize_path_list(normalized.get("clip_paths"), base_dir)
    normalized["sources"] = _normalize_sources(normalized.get("sources"), base_dir)
    return normalized


def _normalize_sources(value: Any, base_dir: Optional[Path]) -> List[Any]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    normalized: List[Any] = []
    for item in items:
        if isinstance(item, dict):
            entry = copy.deepcopy(item)
            for key in ("audio_path", "video_path", "clip_path", "manifest_path", "path"):
                if key in entry and entry[key] is not None:
                    entry[key] = _resolve_path(entry[key], base_dir)
            normalized.append(entry)
        else:
            path = _resolve_path(item, base_dir)
            if path is not None:
                normalized.append(path)
    return normalized


def _normalize_path_list(value: Any, base_dir: Optional[Path]) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        resolved = _resolve_path(item, base_dir)
        if resolved is not None:
            out.append(resolved)
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


def _extract_datasets(raw_catalog: Any, errors: List[str], *, source: Optional[str]) -> List[Dict[str, Any]]:
    if raw_catalog is None:
        errors.append(_format_error(source, "catalog is empty"))
        return []
    if isinstance(raw_catalog, list):
        return list(raw_catalog)
    if isinstance(raw_catalog, dict):
        datasets = raw_catalog.get("datasets")
        if isinstance(datasets, list):
            return list(datasets)
        errors.append(_format_error(source, "catalog.datasets must be a list"))
        return []
    errors.append(_format_error(source, "catalog must be a mapping or a list of datasets"))
    return []


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
