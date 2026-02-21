"""
CONTRACT: inline (source: src/focusfield/core/config.md)
ROLE: Load YAML config, validate, and expose typed accessors.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - config_path: path to YAML file
  - runtime.enable_validation: enable schema validation (bool)

PERF / TIMING:
  - load once at startup

FAILURE MODES:
  - missing/invalid key -> raise error -> log validation_failed

LOG EVENTS:
  - module=core.config, event=validation_failed, payload keys=path, errors

TESTS:
  - tests/contract_tests.md must cover config validation

CONTRACT DETAILS (inline from src/focusfield/core/config.md):
# Config contract

- Config files define build mode, devices, and thresholds.
- Validation must reject missing or inconsistent fields.
- Device profiles reference known geometry and camera HFOV defaults.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def load_config(path: str) -> Dict[str, Any]:
    """Load YAML config and apply defaults."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    defaults = _default_config()
    merged = _merge_dicts(defaults, data)
    _apply_thresholds_preset(merged, path)
    if bool(get_path(merged, "runtime.enable_validation", False)):
        errors = validate_config(merged)
        if errors:
            joined = "\n".join(f"- {e}" for e in errors)
            raise ValueError(f"Config validation failed for {path}:\n{joined}")
    return merged


def get_path(config: Dict[str, Any], dotted_path: str, default: Any = None) -> Any:
    """Get a nested config value by dotted path."""
    node: Any = config
    for key in dotted_path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _default_config() -> Dict[str, Any]:
    return {
        "runtime": {
            "run_id": "",
            "fail_fast": True,
            "enable_validation": False,
            "requirements": {
                "strict": False,
                "min_cameras": 0,
                "min_audio_channels": 0,
            },
            "artifacts": {
                "dir": "artifacts",
                "retention": {
                    "max_runs": 10,
                },
            },
        },
        "ui": {
            "host": "127.0.0.1",
            "port": 8080,
            "telemetry_hz": 15,
        },
        "trace": {
            "enabled": True,
            "level": "medium",
            "thumbnails": {
                "enabled": True,
                "fps": 1,
            },
            "record_raw_audio": True,
            "record_heatmap_full": False,
        },
        "health": {
            "enabled": True,
            "thresholds_ms": {
                "audio_frames": 200,
                "enhanced_final": 300,
                "face_tracks": 1000,
                "camera_frame": 1000,
            },
        },
        "vision": {
            "face": {
                "min_confidence": 0.6,
                "iou_threshold": 0.3,
                "max_missing_frames": 10,
                "min_area": 900,
                "area_soft_max": 3600,
                "min_neighbors": 4,
                "scale_factor": 1.1,
                "detect_width": 360,
                "detect_every_n": 1,
            },
            "track": {
                "smoothing_alpha": 0.6,
                "max_missing_frames": 10,
                "min_age_frames": 2,
            },
            "mouth": {
                "smoothing_alpha": 0.6,
                "min_activity": 0.02,
                "max_activity": 0.2,
                "diff_threshold": 6.0,
                "use_facemesh": True,
                "mesh_every_n": 1,
                "mesh_max_faces": 5,
                "mesh_min_detection_confidence": 0.5,
                "mesh_min_tracking_confidence": 0.5,
                "mesh_min_activity": 0.005,
                "mesh_max_activity": 0.1,
                "mesh_model_path": "",
            },
            "heatmap": {
                "bin_size_deg": 5.0,
                "sigma_deg": 12.0,
                "top_k_peaks": 3,
                "smoothing_alpha": 0.3,
            },
        },
        "fusion": {
            "thresholds_preset": "balanced",
            "weights": {
                "mouth": 0.7,
                "face": 0.3,
                "doa": 0.0,
                "angle": 0.0,
            },
            "audio_fallback": {
                "enabled": True,
                "min_doa_confidence": 0.35,
                "face_staleness_ms": 1200,
            },
            "require_vad": False,
            "vad_max_age_ms": 500,
            "require_speaking": True,
        },
        "bus": {
            "max_queue_depth": 8,
        },
        "logging": {
            "level": "info",
            "file": {
                "enabled": True,
                "flush_interval_ms": 200,
                "rotate_mb": 50,
            },
        },
        "audio": {
            "capture": {
                # If true, allow audio.capture to fall back to mono when the requested
                # multichannel input can't be opened (useful for laptop baseline runs).
                "allow_mono_fallback": True,
            },
            "vad": {
                "enabled": True,
                "mode": 2,
                "frame_ms": 20,
                "min_speech_ratio": 0.3,
            }
        },
        "output": {
            "sink": "file",
            "file_sink": {
                "dir": "artifacts",
                "write_raw_multich": False,
            },
            "virtual_mic": {
                # Prefer a loopback device (macOS: BlackHole/Loopback).
                "channels": 2,
            },
        },
        "perf": {
            "enabled": True,
            "emit_hz": 1.0,
        },
    }


def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _apply_thresholds_preset(config: Dict[str, Any], config_path: str) -> None:
    preset = get_path(config, "fusion.thresholds_preset")
    if not preset:
        return
    preset_path = os.path.join(os.path.dirname(config_path), "thresholds_presets.yaml")
    if not os.path.exists(preset_path):
        return
    with open(preset_path, "r", encoding="utf-8") as handle:
        presets = yaml.safe_load(handle) or {}
    preset_values = presets.get(preset)
    if not preset_values:
        return
    config.setdefault("fusion", {})
    config["fusion"]["thresholds"] = preset_values


def validate_config(config: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors for the merged config.

    This is intentionally lightweight and focuses on catching common "it runs but
    doesn't work" situations early.
    """
    errors: List[str] = []

    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    channels = int(audio_cfg.get("channels", 0) or 0)
    profile_name = str(audio_cfg.get("device_profile", "") or "")
    if channels > 0 and profile_name:
        profile = _load_device_profile(profile_name)
        if profile is None:
            errors.append(f"audio.device_profile '{profile_name}' not found in configs/device_profiles.yaml")
        else:
            order = profile.get("channel_order")
            if isinstance(order, list) and len(order) != channels:
                errors.append(
                    f"audio.channels={channels} but device profile '{profile_name}' has channel_order length {len(order)}"
                )
            geom = str(profile.get("geometry", "") or "").lower()
            if geom == "custom":
                positions = profile.get("positions_m")
                if isinstance(positions, list) and len(positions) != channels:
                    errors.append(
                        f"audio.channels={channels} but device profile '{profile_name}' has positions_m length {len(positions)}"
                    )

    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    req_cfg = runtime_cfg.get("requirements", {})
    if not isinstance(req_cfg, dict):
        req_cfg = {}
    strict = bool(req_cfg.get("strict", False))
    min_cameras = int(req_cfg.get("min_cameras", 0) or 0)
    min_audio_channels = int(req_cfg.get("min_audio_channels", 0) or 0)
    if min_cameras < 0:
        errors.append("runtime.requirements.min_cameras must be >= 0")
    if min_audio_channels < 0:
        errors.append("runtime.requirements.min_audio_channels must be >= 0")
    if strict:
        if channels <= 0:
            errors.append("runtime.requirements.strict=true requires audio.channels > 0")
        if min_audio_channels > 0 and channels < min_audio_channels:
            errors.append(
                f"runtime.requirements.min_audio_channels={min_audio_channels} but audio.channels={channels}"
            )
        video_cfg = config.get("video", {})
        cameras = video_cfg.get("cameras", []) if isinstance(video_cfg, dict) else []
        camera_count = len(cameras) if isinstance(cameras, list) else 0
        if min_cameras > 0 and camera_count < min_cameras:
            errors.append(
                f"runtime.requirements.min_cameras={min_cameras} but video.cameras has {camera_count} entries"
            )

    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    sink = str(output_cfg.get("sink", "") or "").lower()
    if sink in {"virtual_mic", "virtual"}:
        vm = output_cfg.get("virtual_mic")
        if vm is None:
            errors.append("output.sink is virtual_mic but output.virtual_mic is missing")
        elif not isinstance(vm, dict):
            errors.append("output.virtual_mic must be a dict")
        else:
            ch = vm.get("channels")
            if ch is not None and int(ch) <= 0:
                errors.append("output.virtual_mic.channels must be > 0")
            sr = vm.get("sample_rate_hz")
            if sr is not None and int(sr) <= 0:
                errors.append("output.virtual_mic.sample_rate_hz must be > 0")
            device_index = vm.get("device_index")
            if device_index is not None and int(device_index) < 0:
                errors.append("output.virtual_mic.device_index must be >= 0")

    return errors


def _load_device_profile(profile_name: str) -> Optional[Dict[str, Any]]:
    try:
        root = Path(__file__).resolve().parents[3]
        profiles_path = root / "configs" / "device_profiles.yaml"
        with open(profiles_path, "r", encoding="utf-8") as handle:
            profiles = yaml.safe_load(handle) or {}
        mic_arrays = profiles.get("mic_arrays", {}) if isinstance(profiles, dict) else {}
        if not isinstance(mic_arrays, dict):
            return None
        profile = mic_arrays.get(str(profile_name))
        return profile if isinstance(profile, dict) else None
    except Exception:  # noqa: BLE001
        return None
