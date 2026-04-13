"""Runtime camera calibration overlay helpers.

Loads and applies the machine-local `camera_calibration.json` sidecar so UI
calibration and runtime face-bearing calculations share the same yaw source.
"""

from __future__ import annotations

import json
import os
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


CAMERA_CALIBRATION_ENV = "FOCUSFIELD_CAMERA_CALIBRATION_FILE"
CAMERA_CALIBRATION_FILE = "camera_calibration.json"
AUDIO_CALIBRATION_ENV = "FOCUSFIELD_AUDIO_CALIBRATION_FILE"
AUDIO_CALIBRATION_FILE = "audio_calibration.json"


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


def get_audio_calibration_path(base_dir: Path | None = None) -> Path:
    raw = str(os.environ.get(AUDIO_CALIBRATION_ENV, "") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        root = base_dir if base_dir is not None else Path(os.getcwd())
        return (root / candidate).resolve()
    root = base_dir if base_dir is not None else Path(os.getcwd())
    return (root / AUDIO_CALIBRATION_FILE).resolve()


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
                "bearing_model": str(cam.get("bearing_model", "linear") or "linear").lower(),
                "bearing_offset_deg": _safe_float(cam.get("bearing_offset_deg", 0.0), 0.0),
                "bearing_lut_path": str(cam.get("bearing_lut_path", "") or ""),
            }
        )
    return {"cameras": cameras}


def normalize_camera_calibration(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    defaults = build_camera_calibration_defaults(config)
    raw_payload = data.get("cameras", []) if isinstance(data, dict) else data
    normalized, _ = _normalize_camera_calibration_payload(raw_payload, defaults)
    return normalized


def load_camera_calibration(
    config: Dict[str, Any],
    base_dir: Path | None = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    defaults = build_camera_calibration_defaults(config)
    cal_path = get_camera_calibration_path(base_dir=base_dir)
    defaults_by_id = {str(cam["id"]): dict(cam) for cam in defaults["cameras"]}
    configured_camera_ids = [str(cam["id"]) for cam in defaults["cameras"]]
    now_ns = time.time_ns()
    file_stats = _safe_file_stats(cal_path)
    meta: Dict[str, Any] = {
        "path": str(cal_path),
        "active": False,
        "source": "config",
        "status": "missing",
        "loaded_at_ns": now_ns,
        "file_exists": False,
        "file_mtime_ns": None,
        "file_size": None,
        "missing_camera_ids": configured_camera_ids,
        "stale_camera_ids": configured_camera_ids,
        "error_camera_ids": [],
        "unknown_camera_ids": [],
        "duplicate_camera_ids": [],
        "camera_states": [{"camera_id": cam_id, "status": "missing"} for cam_id in configured_camera_ids],
        "applied_camera_ids": [],
        "modified_camera_ids": [],
        "validation": {
            "errors": [],
            "warnings": [],
        },
        "error": "",
    }
    meta["validation"]["warnings"].extend(_validate_hfov_assumptions(config))

    if not cal_path.exists():
        if meta["validation"]["warnings"]:
            meta["status"] = "stale"
        return defaults, meta

    meta["file_exists"] = True
    meta["file_mtime_ns"] = file_stats.get("mtime_ns")
    meta["file_size"] = file_stats.get("size")
    meta["source"] = "sidecar"

    try:
        with cal_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["validation"]["errors"].append(
            {
                "code": "sidecar_read_failed",
                "message": str(exc),
            }
        )
        meta["validation"]["warnings"].extend(_camera_state_warning(configured_camera_ids))
        meta["camera_states"] = [{"camera_id": cam_id, "status": "error"} for cam_id in configured_camera_ids]
        return defaults, meta

    raw_payload = raw.get("cameras", []) if isinstance(raw, dict) else []
    normalized, validation = _normalize_camera_calibration_payload(raw_payload, defaults, sidecar_dir=cal_path.parent)

    meta["validation"]["errors"].extend(validation["errors"])
    meta["validation"]["warnings"].extend(validation["warnings"])

    seen_camera_ids = set(validation["seen_camera_ids"])
    applied_camera_ids = set(validation["applied_camera_ids"])
    error_camera_ids = set(validation["error_camera_ids"])
    missing_camera_ids = [
        cam_id for cam_id in configured_camera_ids if cam_id not in seen_camera_ids and cam_id not in error_camera_ids
    ]
    camera_states: List[Dict[str, Any]] = []
    for cam_id in configured_camera_ids:
        if cam_id in error_camera_ids:
            camera_states.append({"camera_id": cam_id, "status": "error"})
        elif cam_id in applied_camera_ids:
            camera_states.append({"camera_id": cam_id, "status": "active"})
        else:
            camera_states.append({"camera_id": cam_id, "status": "stale"})

    meta["camera_states"] = camera_states
    meta["missing_camera_ids"] = missing_camera_ids
    meta["error_camera_ids"] = list(error_camera_ids)
    meta["stale_camera_ids"] = [state["camera_id"] for state in camera_states if state["status"] == "stale"]
    meta["unknown_camera_ids"] = list(validation["unknown_camera_ids"])
    meta["duplicate_camera_ids"] = list(validation["duplicate_camera_ids"])
    meta["applied_camera_ids"] = list(validation["applied_camera_ids"])
    modified = [
        str(cam["id"])
        for cam in normalized["cameras"]
        if (
            _safe_float(cam.get("yaw_offset_deg", 0.0), 0.0)
            != _safe_float(defaults_by_id.get(str(cam["id"]), {}).get("yaw_offset_deg", 0.0), 0.0)
            or _safe_float(cam.get("bearing_offset_deg", 0.0), 0.0)
            != _safe_float(defaults_by_id.get(str(cam["id"]), {}).get("bearing_offset_deg", 0.0), 0.0)
            or str(cam.get("bearing_model", "linear") or "linear").lower()
            != str(defaults_by_id.get(str(cam["id"]), {}).get("bearing_model", "linear") or "linear").lower()
            or str(cam.get("bearing_lut_path", "") or "")
            != str(defaults_by_id.get(str(cam["id"]), {}).get("bearing_lut_path", "") or "")
        )
    ]
    meta["modified_camera_ids"] = modified
    meta["active"] = True

    if validation["errors"]:
        meta["status"] = "error"
    elif (
        meta["validation"]["warnings"]
        or meta["missing_camera_ids"]
        or meta["stale_camera_ids"]
        or meta["unknown_camera_ids"]
        or meta["duplicate_camera_ids"]
    ):
        meta["status"] = "stale"
    else:
        meta["status"] = "active"

    return normalized, meta


def save_camera_calibration(
    data: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: Path | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    cal_path = get_camera_calibration_path(base_dir=base_dir)
    raw_payload = data.get("cameras", []) if isinstance(data, dict) else data
    normalized, _validation = _normalize_camera_calibration_payload(
        raw_payload,
        build_camera_calibration_defaults(config),
        sidecar_dir=cal_path.parent,
    )
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
        cam["bearing_offset_deg"] = _safe_float(
            overlay.get("bearing_offset_deg", cam.get("bearing_offset_deg", 0.0)),
            _safe_float(cam.get("bearing_offset_deg", 0.0), 0.0),
        )
        cam["bearing_model"] = str(overlay.get("bearing_model", cam.get("bearing_model", "linear")) or "linear").lower()
        cam["bearing_lut_path"] = str(overlay.get("bearing_lut_path", cam.get("bearing_lut_path", "")) or "")
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


def load_audio_calibration(
    config: Dict[str, Any],
    base_dir: Path | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    audio_cfg = config.get("audio", {}) if isinstance(config, dict) else {}
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    base_runtime_yaw = _safe_float(audio_cfg.get("yaw_offset_deg", 0.0), 0.0)
    cal_path = get_audio_calibration_path(base_dir=base_dir)
    now_ns = time.time_ns()
    file_stats = _safe_file_stats(cal_path)
    meta: Dict[str, Any] = {
        "path": str(cal_path),
        "active": False,
        "source": "config",
        "status": "missing",
        "loaded_at_ns": now_ns,
        "file_exists": False,
        "file_mtime_ns": None,
        "file_size": None,
        "reload_behavior": "startup_only",
        "hot_reload_supported": False,
        "restart_required_on_change": True,
        "base_runtime_yaw_offset_deg": base_runtime_yaw,
        "sidecar_yaw_offset_deg": 0.0,
        "effective_runtime_yaw_offset_deg": base_runtime_yaw,
        "validation": {"errors": [], "warnings": []},
        "error": "",
    }
    calibration = {"yaw_offset_deg": base_runtime_yaw}
    if not cal_path.exists():
        return calibration, meta

    meta["file_exists"] = True
    meta["file_mtime_ns"] = file_stats.get("mtime_ns")
    meta["file_size"] = file_stats.get("size")
    meta["source"] = "sidecar"
    try:
        with cal_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["validation"]["errors"].append(
            {
                "code": "sidecar_read_failed",
                "message": str(exc),
            }
        )
        return calibration, meta

    if not isinstance(raw, dict):
        meta["status"] = "error"
        meta["validation"]["errors"].append(
            {
                "code": "audio_calibration_invalid",
                "message": "audio calibration sidecar must be a JSON object",
            }
        )
        return calibration, meta

    parsed_yaw = _parse_finite_float(raw.get("yaw_offset_deg"))
    if parsed_yaw is None:
        meta["status"] = "error"
        meta["validation"]["errors"].append(
            {
                "code": "yaw_invalid",
                "message": "non-finite or non-numeric yaw_offset_deg",
            }
        )
        return calibration, meta

    effective_runtime_yaw = base_runtime_yaw + parsed_yaw
    calibration = {"yaw_offset_deg": effective_runtime_yaw}
    meta["active"] = True
    meta["status"] = "active"
    meta["sidecar_yaw_offset_deg"] = parsed_yaw
    meta["effective_runtime_yaw_offset_deg"] = effective_runtime_yaw
    return calibration, meta


def apply_audio_calibration(config: Dict[str, Any], calibration: Dict[str, Any]) -> float:
    audio_cfg = config.setdefault("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
        config["audio"] = audio_cfg
    effective_runtime_yaw = _safe_float(calibration.get("yaw_offset_deg", audio_cfg.get("yaw_offset_deg", 0.0)), 0.0)
    audio_cfg["yaw_offset_deg"] = effective_runtime_yaw
    return effective_runtime_yaw


def apply_audio_calibration_sidecar(config: Dict[str, Any], base_dir: Path | None = None) -> Dict[str, Any]:
    calibration, meta = load_audio_calibration(config, base_dir=base_dir)
    effective_runtime_yaw = apply_audio_calibration(config, calibration)
    runtime_cfg = config.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        config["runtime"] = runtime_cfg
    runtime_cfg["audio_calibration_overlay"] = {
        **meta,
        "effective_runtime_yaw_offset_deg": effective_runtime_yaw,
    }
    return runtime_cfg["audio_calibration_overlay"]


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_camera_calibration_payload(
    payload: Any,
    defaults: Dict[str, List[Dict[str, Any]]],
    sidecar_dir: Path | None = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    defaults_by_id = {str(cam["id"]): dict(cam) for cam in defaults.get("cameras", [])}
    raw_cameras = payload if isinstance(payload, list) else []
    if not isinstance(raw_cameras, list):
        raw_cameras = []

    updates: Dict[str, Dict[str, Any]] = {}
    seen_camera_ids: set[str] = set()
    unknown_camera_ids: set[str] = set()
    duplicate_camera_ids: set[str] = set()
    error_camera_ids: set[str] = set()
    applied_camera_ids: set[str] = set()
    issues: Dict[str, List[Dict[str, Any]]] = {"errors": [], "warnings": []}

    for item in raw_cameras:
        if not isinstance(item, dict):
            issues["errors"].append(
                {
                    "code": "camera_entry_invalid",
                    "message": "camera entry is not an object",
                }
            )
            continue

        camera_id = str(item.get("id", "") or "").strip()
        if not camera_id:
            issues["errors"].append(
                {
                    "code": "camera_id_missing",
                    "message": "camera entry is missing id",
                }
            )
            continue

        is_configured = camera_id in defaults_by_id
        if not is_configured:
            unknown_camera_ids.add(camera_id)
            continue

        if camera_id in seen_camera_ids:
            duplicate_camera_ids.add(camera_id)
        seen_camera_ids.add(camera_id)

        if "yaw_offset_deg" in item:
            parsed_yaw = _parse_finite_float(item.get("yaw_offset_deg"))
            if parsed_yaw is None:
                error_camera_ids.add(camera_id)
                issues["errors"].append(
                    {
                        "code": "yaw_invalid",
                        "camera_id": camera_id,
                        "message": "non-finite or non-numeric yaw_offset_deg",
                    }
                )
                continue
            normalized_yaw = _normalize_yaw(parsed_yaw)
            if normalized_yaw != parsed_yaw:
                issues["warnings"].append(
                    {
                        "code": "yaw_normalized",
                        "camera_id": camera_id,
                        "from": parsed_yaw,
                        "to": normalized_yaw,
                    }
                )
        else:
            normalized_yaw = _safe_float(defaults_by_id[camera_id]["yaw_offset_deg"], 0.0)

        current = dict(defaults_by_id[camera_id])
        current["yaw_offset_deg"] = normalized_yaw
        current["bearing_model"] = str(current.get("bearing_model", "linear") or "linear").lower()

        if "bearing_offset_deg" in item:
            parsed_bearing_offset = _parse_finite_float(item.get("bearing_offset_deg"))
            if parsed_bearing_offset is None:
                error_camera_ids.add(camera_id)
                issues["errors"].append(
                    {
                        "code": "bearing_offset_invalid",
                        "camera_id": camera_id,
                        "message": "non-finite or non-numeric bearing_offset_deg",
                    }
                )
                continue
            current["bearing_offset_deg"] = parsed_bearing_offset

        if "bearing_model" in item:
            bearing_model = str(item.get("bearing_model", current.get("bearing_model", "linear")) or "linear").strip().lower()
            current["bearing_model"] = bearing_model or "linear"

        if "bearing_lut_path" in item:
            current["bearing_lut_path"] = _resolve_sidecar_relative_path(item.get("bearing_lut_path"), sidecar_dir)

        updates[camera_id] = current
        applied_camera_ids.add(camera_id)

    normalized = {
        "cameras": [updates.get(str(cam["id"]), dict(cam)) for cam in defaults.get("cameras", [])],
    }

    return normalized, {
        "errors": issues["errors"],
        "warnings": issues["warnings"],
        "applied_camera_ids": sorted(applied_camera_ids),
        "error_camera_ids": sorted(error_camera_ids),
        "unknown_camera_ids": sorted(unknown_camera_ids),
        "duplicate_camera_ids": sorted(duplicate_camera_ids),
        "seen_camera_ids": sorted(seen_camera_ids),
    }


def _parse_finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _normalize_yaw(yaw_deg: float) -> float:
    return (yaw_deg % 360.0 + 360.0) % 360.0


def _validate_hfov_assumptions(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    cameras_cfg = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras_cfg, list):
        cameras_cfg = []
    warnings: List[Dict[str, Any]] = []
    for idx, cam in enumerate(cameras_cfg):
        if not isinstance(cam, dict):
            continue
        camera_id = str(cam.get("id", f"cam{idx}"))
        if "hfov_deg" not in cam:
            continue
        parsed_hfov = _parse_finite_float(cam.get("hfov_deg"))
        if parsed_hfov is None or parsed_hfov <= 0.0 or parsed_hfov >= 360.0:
            warnings.append(
                {
                    "code": "hfov_suspicious_range",
                    "camera_id": camera_id,
                    "value": cam.get("hfov_deg"),
                    "message": "hfov_deg should be within canonical 0<hfov<360 range",
                }
            )
    return warnings


def _safe_file_stats(path: Path) -> Dict[str, int | None]:
    try:
        stat = path.stat()
    except OSError:
        return {"mtime_ns": None, "size": None}
    return {
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _camera_state_warning(camera_ids: List[str]) -> List[Dict[str, Any]]:
    return [
        {
            "code": "camera_state",
            "camera_id": camera_id,
            "message": "camera calibration state requires refresh",
        }
        for camera_id in camera_ids
    ]


def _resolve_sidecar_relative_path(value: object, sidecar_dir: Path | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    root = sidecar_dir if sidecar_dir is not None else Path(os.getcwd())
    return str((root / candidate).resolve())
