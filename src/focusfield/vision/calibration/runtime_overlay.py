"""Runtime camera calibration overlay helpers.

Loads and applies the machine-local `camera_calibration.json` sidecar so UI
calibration and runtime face-bearing calculations share the same yaw source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple


CAMERA_CALIBRATION_ENV = "FOCUSFIELD_CAMERA_CALIBRATION_FILE"
CAMERA_CALIBRATION_FILE = "camera_calibration.json"


def get_camera_calibration_path(base_dir: Path | None = None) -> Path:
    raw = str(os.environ.get(CAMERA_CALIBRATION_ENV, "") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        root = base_dir if base_dir is not None else Path(os.getcwd())
        return (root / candidate).resolve()
    root = base_dir if base_dir is not None else Path(os.getcwd())
    return (root / CAMERA_CALIBRATION_FILE).resolve()


def build_camera_calibration_defaults(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    cameras_cfg = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras_cfg, list):
        cameras_cfg = []
    cameras: List[Dict[str, Any]] = []
    for idx, cam in enumerate(cameras_cfg):
        if not isinstance(cam, dict):
            continue
        cameras.append(
            {
                "id": str(cam.get("id", f"cam{idx}")),
                "yaw_offset_deg": _safe_float(cam.get("yaw_offset_deg", 0.0), 0.0),
            }
        )
    return {"cameras": cameras}


def normalize_camera_calibration(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    defaults = build_camera_calibration_defaults(config)
    defaults_by_id = {str(cam["id"]): dict(cam) for cam in defaults["cameras"]}
    if not defaults_by_id:
        return {"cameras": []}

    raw_cameras = data.get("cameras", []) if isinstance(data, dict) else []
    if not isinstance(raw_cameras, list):
        raw_cameras = []

    updates: Dict[str, Dict[str, Any]] = {}
    for item in raw_cameras:
        if not isinstance(item, dict):
            continue
        camera_id = str(item.get("id", "") or "").strip()
        if not camera_id or camera_id not in defaults_by_id:
            continue
        current = dict(defaults_by_id[camera_id])
        current["yaw_offset_deg"] = _safe_float(item.get("yaw_offset_deg", current["yaw_offset_deg"]), current["yaw_offset_deg"])
        updates[camera_id] = current

    return {
        "cameras": [updates.get(str(cam["id"]), dict(cam)) for cam in defaults["cameras"]],
    }


def load_camera_calibration(
    config: Dict[str, Any],
    base_dir: Path | None = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    defaults = build_camera_calibration_defaults(config)
    cal_path = get_camera_calibration_path(base_dir=base_dir)
    meta: Dict[str, Any] = {
        "path": str(cal_path),
        "active": False,
        "source": "config",
        "modified_camera_ids": [],
        "error": "",
    }
    if not cal_path.exists():
        return defaults, meta
    try:
        with cal_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        meta["error"] = str(exc)
        return defaults, meta
    normalized = normalize_camera_calibration(raw if isinstance(raw, dict) else {}, config)
    defaults_by_id = {str(cam["id"]): cam for cam in defaults["cameras"]}
    modified = [
        str(cam["id"])
        for cam in normalized["cameras"]
        if _safe_float(cam.get("yaw_offset_deg", 0.0), 0.0)
        != _safe_float(defaults_by_id.get(str(cam["id"]), {}).get("yaw_offset_deg", 0.0), 0.0)
    ]
    meta["active"] = True
    meta["source"] = "sidecar"
    meta["modified_camera_ids"] = modified
    return normalized, meta


def save_camera_calibration(
    data: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: Path | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    normalized = normalize_camera_calibration(data, config)
    cal_path = get_camera_calibration_path(base_dir=base_dir)
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    with cal_path.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, indent=2)
    return normalized


def apply_camera_calibration(config: Dict[str, Any], calibration: Dict[str, Any]) -> List[str]:
    cameras_cfg = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras_cfg, list):
        return []
    calibration_by_id = {
        str(item.get("id", "")): item
        for item in calibration.get("cameras", [])
        if isinstance(item, dict)
    }
    applied: List[str] = []
    for idx, cam in enumerate(cameras_cfg):
        if not isinstance(cam, dict):
            continue
        camera_id = str(cam.get("id", f"cam{idx}"))
        overlay = calibration_by_id.get(camera_id)
        if overlay is None:
            continue
        cam["yaw_offset_deg"] = _safe_float(overlay.get("yaw_offset_deg", cam.get("yaw_offset_deg", 0.0)), 0.0)
        applied.append(camera_id)
    return applied


def apply_camera_calibration_sidecar(config: Dict[str, Any], base_dir: Path | None = None) -> Dict[str, Any]:
    calibration, meta = load_camera_calibration(config, base_dir=base_dir)
    applied = apply_camera_calibration(config, calibration)
    runtime_cfg = config.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        config["runtime"] = runtime_cfg
    runtime_cfg["camera_calibration_overlay"] = {
        **meta,
        "applied_camera_ids": applied,
        "cameras": calibration.get("cameras", []),
    }
    return runtime_cfg["camera_calibration_overlay"]


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)
