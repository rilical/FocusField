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
            "perf_profile": "default",
            "enable_validation": False,
            "scheduling": {
                "niceness": 0,
                "enable_rt": False,
                "rt_priority": 70,
            },
            "alignment": {
                "zero_deg_reference": "cam0_usb",
                "require_calibrated_mic_yaw": False,
            },
            "requirements": {
                "strict": False,
                "min_cameras": 0,
                "min_audio_channels": 0,
                "camera_scope": "any",
                "require_led_hid": False,
                "require_led_hid_runtime_check": True,
            },
            "artifacts": {
                "dir": "artifacts",
                "retention": {
                    "max_runs": 10,
                },
            },
        },
        "ui": {
            "host": "0.0.0.0",
            "port": 8080,
            "telemetry_hz": 15,
            "frame_jpeg_quality": 65,
            "frame_max_hz": 6.0,
        },
        "uma8_leds": {
            "enabled": False,
            "enabled_fallback": True,
            "strict_transport": False,
            "backend": "simulate",
            "ring_size": 12,
            "update_hz": 12.0,
            "base_bearing_offset_deg": 0.0,
            "idle_rgb": [10, 10, 24],
            "lock_rgb": [0, 255, 128],
            "search_rgb": [0, 140, 255],
            "smoothing_alpha": 0.35,
            "brightness_min": 0.05,
            "brightness_max": 0.85,
            "vendor_id": 10066,
            "product_id": 28,
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
                "detector_backend": "haar",
                "min_confidence": 0.6,
                "iou_threshold": 0.3,
                "max_missing_frames": 10,
                "min_area": 900,
                "area_soft_max": 3600,
                "min_neighbors": 4,
                "scale_factor": 1.1,
                "detect_width": 360,
                "detect_every_n": 1,
                "full_frame_every_n": 4,
                "roi_margin_ratio": 0.2,
                "max_rois_per_frame": 4,
                "yunet": {
                    "model_path": "",
                    "auto_download": True,
                    "score_threshold": 0.75,
                    "nms_threshold": 0.3,
                    "top_k": 5000,
                    "input_width": 320,
                    "input_height": 320,
                },
                "preprocess": {
                    "enabled": False,
                    "clahe_clip_limit": 2.0,
                    "clahe_tile": 8,
                    "gamma": 1.0,
                    "blur_kernel": 0,
                },
                "pose": {
                    "disconnect_angle_deg": 55.0,
                    "reconnect_angle_deg": 42.0,
                    "off_angle_drop_ms": 1200.0,
                    "decay_alpha": 0.35,
                },
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
                "mouth": 0.42,
                "face": 0.18,
                "doa": 0.28,
                "angle": 0.12,
            },
            "interruption": {
                "handoff_margin": 0.06,
                "interrupt_min_delta": 0.04,
                "interrupt_hold_ms": 300.0,
                "score_smoothing_alpha": 0.45,
            },
            "audio_fallback": {
                "enabled": True,
                "min_doa_confidence": 0.35,
                "min_peak_score": 0.22,
                "score_mode": "max",
                "require_vad": False,
                "allow_when_faces_missing": True,
                "face_staleness_ms": 1200,
            },
            "require_vad": False,
            "vad_max_age_ms": 500,
            "require_speaking": True,
        },
        "bus": {
            "max_queue_depth": 8,
            "topic_queue_depths": {},
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
                "portaudio_latency": "high",
                "status_log_interval_s": 1.0,
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
        "bench": {
            "targets": {
                "si_sdr_delta_db_min": 2.0,
                "stoi_delta_min": 0.03,
                "wer_relative_improvement_min": 0.12,
                "sir_delta_db_min": 4.0,
                "latency_p95_ms_max": 150.0,
                "latency_p99_ms_max": 220.0,
                "audio_queue_full_max": 25.0,
                "audio_underrun_rate_max": 0.005,
            }
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
    perf_profile = str(runtime_cfg.get("perf_profile", "default") or "default").strip().lower()
    if perf_profile not in {"default", "realtime_pi_max"}:
        errors.append("runtime.perf_profile must be one of: default, realtime_pi_max")
    min_cameras = int(req_cfg.get("min_cameras", 0) or 0)
    min_audio_channels = int(req_cfg.get("min_audio_channels", 0) or 0)
    camera_scope = str(req_cfg.get("camera_scope", "any") or "any").strip().lower()
    if camera_scope not in {"any", "usb"}:
        errors.append("runtime.requirements.camera_scope must be one of: any, usb")
    if "require_led_hid" in req_cfg and not isinstance(req_cfg.get("require_led_hid"), bool):
        errors.append("runtime.requirements.require_led_hid must be bool")
    if "require_led_hid_runtime_check" in req_cfg and not isinstance(req_cfg.get("require_led_hid_runtime_check"), bool):
        errors.append("runtime.requirements.require_led_hid_runtime_check must be bool")
    if min_cameras < 0:
        errors.append("runtime.requirements.min_cameras must be >= 0")
    if min_audio_channels < 0:
        errors.append("runtime.requirements.min_audio_channels must be >= 0")
    scheduling_cfg = runtime_cfg.get("scheduling", {})
    if scheduling_cfg is not None and not isinstance(scheduling_cfg, dict):
        errors.append("runtime.scheduling must be a mapping when provided")
        scheduling_cfg = {}
    if isinstance(scheduling_cfg, dict) and "niceness" in scheduling_cfg:
        try:
            _ = int(scheduling_cfg.get("niceness"))
        except Exception:
            errors.append("runtime.scheduling.niceness must be integer")
    if isinstance(scheduling_cfg, dict) and "enable_rt" in scheduling_cfg and not isinstance(scheduling_cfg.get("enable_rt"), bool):
        errors.append("runtime.scheduling.enable_rt must be bool")
    if isinstance(scheduling_cfg, dict) and "rt_priority" in scheduling_cfg:
        try:
            rt_priority = int(scheduling_cfg.get("rt_priority"))
        except Exception:
            errors.append("runtime.scheduling.rt_priority must be integer")
        else:
            if rt_priority < 1 or rt_priority > 99:
                errors.append("runtime.scheduling.rt_priority must be in [1, 99]")
    alignment_cfg = runtime_cfg.get("alignment", {})
    if alignment_cfg is not None and not isinstance(alignment_cfg, dict):
        errors.append("runtime.alignment must be a mapping when provided")
        alignment_cfg = {}
    if isinstance(alignment_cfg, dict):
        if "zero_deg_reference" in alignment_cfg:
            zero_ref = str(alignment_cfg.get("zero_deg_reference", "") or "").strip().lower()
            if zero_ref not in {"cam0_usb", "front_center", "manual_calibration"}:
                errors.append(
                    "runtime.alignment.zero_deg_reference must be one of: cam0_usb, front_center, manual_calibration"
                )
        if "require_calibrated_mic_yaw" in alignment_cfg and not isinstance(
            alignment_cfg.get("require_calibrated_mic_yaw"), bool
        ):
            errors.append("runtime.alignment.require_calibrated_mic_yaw must be bool")
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

    bus_cfg = config.get("bus", {})
    if not isinstance(bus_cfg, dict):
        bus_cfg = {}
    topic_queue_depths = bus_cfg.get("topic_queue_depths", {})
    if topic_queue_depths is not None and not isinstance(topic_queue_depths, dict):
        errors.append("bus.topic_queue_depths must be a mapping when provided")
    elif isinstance(topic_queue_depths, dict):
        for topic, depth in topic_queue_depths.items():
            try:
                depth_i = int(depth)
            except Exception:
                errors.append(f"bus.topic_queue_depths.{topic} must be integer")
                continue
            if depth_i <= 0:
                errors.append(f"bus.topic_queue_depths.{topic} must be > 0")

    ui_cfg = config.get("ui", {})
    if ui_cfg is not None and not isinstance(ui_cfg, dict):
        errors.append("ui must be a mapping when provided")
        ui_cfg = {}
    if isinstance(ui_cfg, dict):
        if "frame_jpeg_quality" in ui_cfg:
            try:
                q = int(ui_cfg.get("frame_jpeg_quality", 65))
            except Exception:
                errors.append("ui.frame_jpeg_quality must be integer")
            else:
                if q < 1 or q > 100:
                    errors.append("ui.frame_jpeg_quality must be in [1, 100]")
        if "frame_max_hz" in ui_cfg:
            try:
                hz = float(ui_cfg.get("frame_max_hz", 6.0))
            except Exception:
                errors.append("ui.frame_max_hz must be numeric")
            else:
                if hz <= 0.0:
                    errors.append("ui.frame_max_hz must be > 0")

    beam_cfg = audio_cfg.get("beamformer", {})
    capture_cfg = audio_cfg.get("capture", {})
    if capture_cfg is not None and not isinstance(capture_cfg, dict):
        errors.append("audio.capture must be a mapping when provided")
        capture_cfg = {}
    if isinstance(capture_cfg, dict):
        latency_value = capture_cfg.get("portaudio_latency")
        if latency_value is not None:
            if isinstance(latency_value, str):
                if latency_value.strip().lower() not in {"low", "high"}:
                    errors.append("audio.capture.portaudio_latency must be 'low', 'high', or numeric seconds")
            else:
                try:
                    latency_num = float(latency_value)
                except Exception:
                    errors.append("audio.capture.portaudio_latency must be 'low', 'high', or numeric seconds")
                else:
                    if latency_num <= 0.0:
                        errors.append("audio.capture.portaudio_latency numeric value must be > 0")
        if "status_log_interval_s" in capture_cfg:
            try:
                status_interval = float(capture_cfg.get("status_log_interval_s"))
            except Exception:
                errors.append("audio.capture.status_log_interval_s must be numeric")
            else:
                if status_interval <= 0.0:
                    errors.append("audio.capture.status_log_interval_s must be > 0")

    vision_cfg = config.get("vision", {})
    if vision_cfg is not None and not isinstance(vision_cfg, dict):
        errors.append("vision must be a mapping when provided")
        vision_cfg = {}
    if isinstance(vision_cfg, dict):
        face_cfg = vision_cfg.get("face", {})
        if face_cfg is not None and not isinstance(face_cfg, dict):
            errors.append("vision.face must be a mapping when provided")
            face_cfg = {}
        if isinstance(face_cfg, dict):
            detector_backend = str(face_cfg.get("detector_backend", "haar") or "haar").strip().lower()
            if detector_backend not in {"haar", "yunet", "blazeface"}:
                errors.append("vision.face.detector_backend must be one of: haar, yunet, blazeface")

            for key in ("full_frame_every_n", "max_rois_per_frame"):
                if key in face_cfg:
                    try:
                        value = int(face_cfg.get(key))
                    except Exception:
                        errors.append(f"vision.face.{key} must be integer")
                        continue
                    if value < 1:
                        errors.append(f"vision.face.{key} must be >= 1")
            if "roi_margin_ratio" in face_cfg:
                try:
                    value = float(face_cfg.get("roi_margin_ratio"))
                except Exception:
                    errors.append("vision.face.roi_margin_ratio must be numeric")
                else:
                    if value < 0.0 or value > 1.0:
                        errors.append("vision.face.roi_margin_ratio must be in [0, 1]")

            yunet_cfg = face_cfg.get("yunet", {})
            if yunet_cfg is not None and not isinstance(yunet_cfg, dict):
                errors.append("vision.face.yunet must be a mapping when provided")
                yunet_cfg = {}
            if isinstance(yunet_cfg, dict):
                if "model_path" in yunet_cfg and not isinstance(yunet_cfg.get("model_path"), str):
                    errors.append("vision.face.yunet.model_path must be string")
                if "auto_download" in yunet_cfg and not isinstance(yunet_cfg.get("auto_download"), bool):
                    errors.append("vision.face.yunet.auto_download must be bool")
                for key in ("score_threshold", "nms_threshold"):
                    if key in yunet_cfg:
                        try:
                            value = float(yunet_cfg.get(key))
                        except Exception:
                            errors.append(f"vision.face.yunet.{key} must be numeric")
                            continue
                        if value <= 0.0 or value > 1.0:
                            errors.append(f"vision.face.yunet.{key} must be in (0, 1]")
                for key in ("top_k", "input_width", "input_height"):
                    if key in yunet_cfg:
                        try:
                            value = int(yunet_cfg.get(key))
                        except Exception:
                            errors.append(f"vision.face.yunet.{key} must be integer")
                            continue
                        if value < 1:
                            errors.append(f"vision.face.yunet.{key} must be >= 1")

            preprocess_cfg = face_cfg.get("preprocess", {})
            if preprocess_cfg is not None and not isinstance(preprocess_cfg, dict):
                errors.append("vision.face.preprocess must be a mapping when provided")
                preprocess_cfg = {}
            if isinstance(preprocess_cfg, dict):
                if "enabled" in preprocess_cfg and not isinstance(preprocess_cfg.get("enabled"), bool):
                    errors.append("vision.face.preprocess.enabled must be bool")
                if "clahe_clip_limit" in preprocess_cfg:
                    try:
                        value = float(preprocess_cfg.get("clahe_clip_limit"))
                    except Exception:
                        errors.append("vision.face.preprocess.clahe_clip_limit must be numeric")
                    else:
                        if value <= 0.0:
                            errors.append("vision.face.preprocess.clahe_clip_limit must be > 0")
                for key in ("clahe_tile", "blur_kernel"):
                    if key in preprocess_cfg:
                        try:
                            value = int(preprocess_cfg.get(key))
                        except Exception:
                            errors.append(f"vision.face.preprocess.{key} must be integer")
                            continue
                        if key == "clahe_tile" and value < 1:
                            errors.append("vision.face.preprocess.clahe_tile must be >= 1")
                        if key == "blur_kernel":
                            if value < 0:
                                errors.append("vision.face.preprocess.blur_kernel must be >= 0")
                            elif value > 0 and value % 2 == 0:
                                errors.append("vision.face.preprocess.blur_kernel must be odd when > 0")
                if "gamma" in preprocess_cfg:
                    try:
                        value = float(preprocess_cfg.get("gamma"))
                    except Exception:
                        errors.append("vision.face.preprocess.gamma must be numeric")
                    else:
                        if value <= 0.0:
                            errors.append("vision.face.preprocess.gamma must be > 0")
            pose_cfg = face_cfg.get("pose", {})
            if pose_cfg is not None and not isinstance(pose_cfg, dict):
                errors.append("vision.face.pose must be a mapping when provided")
                pose_cfg = {}
            if isinstance(pose_cfg, dict):
                for key in ("disconnect_angle_deg", "reconnect_angle_deg", "off_angle_drop_ms"):
                    if key in pose_cfg:
                        try:
                            value = float(pose_cfg.get(key))
                        except Exception:
                            errors.append(f"vision.face.pose.{key} must be numeric")
                            continue
                        if value <= 0.0:
                            errors.append(f"vision.face.pose.{key} must be > 0")
                if "decay_alpha" in pose_cfg:
                    try:
                        value = float(pose_cfg.get("decay_alpha"))
                    except Exception:
                        errors.append("vision.face.pose.decay_alpha must be numeric")
                    else:
                        if value < 0.0 or value > 1.0:
                            errors.append("vision.face.pose.decay_alpha must be in [0, 1]")
                disconnect = float(pose_cfg.get("disconnect_angle_deg", 55.0) or 55.0)
                reconnect = float(pose_cfg.get("reconnect_angle_deg", 42.0) or 42.0)
                if reconnect >= disconnect:
                    errors.append("vision.face.pose.reconnect_angle_deg must be < disconnect_angle_deg")

    video_cfg = config.get("video", {})
    if isinstance(video_cfg, dict):
        cameras_cfg = video_cfg.get("cameras", [])
        if isinstance(cameras_cfg, list):
            for idx, camera_cfg in enumerate(cameras_cfg):
                if not isinstance(camera_cfg, dict):
                    continue
                controls_cfg = camera_cfg.get("controls")
                if controls_cfg is None:
                    continue
                if not isinstance(controls_cfg, dict):
                    errors.append(f"video.cameras[{idx}].controls must be a mapping when provided")
                    continue
                for key in ("auto_exposure", "exposure", "gain", "brightness", "contrast"):
                    if key in controls_cfg:
                        try:
                            float(controls_cfg.get(key))
                        except Exception:
                            errors.append(f"video.cameras[{idx}].controls.{key} must be numeric")

    if isinstance(beam_cfg, dict):
        mvdr_cfg = beam_cfg.get("mvdr", {})
        if not isinstance(mvdr_cfg, dict):
            mvdr_cfg = {}
        weight_interp_alpha = float(mvdr_cfg.get("weight_interp_alpha", 0.35) or 0.35)
        if not 0.0 < weight_interp_alpha <= 1.0:
            errors.append("audio.beamformer.mvdr.weight_interp_alpha must be in (0, 1]")
        freeze_cov = mvdr_cfg.get("speech_freeze_covariance", True)
        if not isinstance(freeze_cov, bool):
            errors.append("audio.beamformer.mvdr.speech_freeze_covariance must be bool")
        freq_low_hz = float(mvdr_cfg.get("freq_low_hz", 120.0) or 120.0)
        freq_high_hz = float(mvdr_cfg.get("freq_high_hz", 4800.0) or 4800.0)
        if freq_low_hz < 0.0:
            errors.append("audio.beamformer.mvdr.freq_low_hz must be >= 0")
        if freq_high_hz <= freq_low_hz:
            errors.append("audio.beamformer.mvdr.freq_high_hz must be > freq_low_hz")

    fusion_cfg = config.get("fusion", {})
    if fusion_cfg is not None and not isinstance(fusion_cfg, dict):
        errors.append("fusion must be a mapping when provided")
        fusion_cfg = {}
    if isinstance(fusion_cfg, dict):
        thresholds_cfg = fusion_cfg.get("thresholds", {})
        if thresholds_cfg is not None and not isinstance(thresholds_cfg, dict):
            errors.append("fusion.thresholds must be a mapping when provided")
            thresholds_cfg = {}
        if isinstance(thresholds_cfg, dict):
            for key in ("acquire_threshold_audio_only", "drop_threshold_audio_only"):
                if key in thresholds_cfg:
                    try:
                        value = float(thresholds_cfg.get(key))
                    except Exception:
                        errors.append(f"fusion.thresholds.{key} must be numeric")
                        continue
                    if value < 0.0 or value > 1.0:
                        errors.append(f"fusion.thresholds.{key} must be in [0, 1]")
            for key in ("hold_ms_audio_only", "handoff_min_ms_audio_only"):
                if key in thresholds_cfg:
                    try:
                        value = float(thresholds_cfg.get(key))
                    except Exception:
                        errors.append(f"fusion.thresholds.{key} must be numeric")
                        continue
                    if value <= 0.0:
                        errors.append(f"fusion.thresholds.{key} must be > 0")

        fallback_cfg = fusion_cfg.get("audio_fallback", {})
        if fallback_cfg is not None and not isinstance(fallback_cfg, dict):
            errors.append("fusion.audio_fallback must be a mapping when provided")
            fallback_cfg = {}
        if isinstance(fallback_cfg, dict):
            for key in ("enabled", "require_vad", "allow_when_faces_missing"):
                if key in fallback_cfg and not isinstance(fallback_cfg.get(key), bool):
                    errors.append(f"fusion.audio_fallback.{key} must be bool")
            for key in ("min_doa_confidence", "min_peak_score"):
                if key in fallback_cfg:
                    try:
                        value = float(fallback_cfg.get(key))
                    except Exception:
                        errors.append(f"fusion.audio_fallback.{key} must be numeric")
                        continue
                    if not 0.0 <= value <= 1.0:
                        errors.append(f"fusion.audio_fallback.{key} must be in [0, 1]")
            if "face_staleness_ms" in fallback_cfg:
                try:
                    staleness = float(fallback_cfg.get("face_staleness_ms"))
                except Exception:
                    errors.append("fusion.audio_fallback.face_staleness_ms must be numeric")
                else:
                    if staleness <= 0.0:
                        errors.append("fusion.audio_fallback.face_staleness_ms must be > 0")
            score_mode = str(fallback_cfg.get("score_mode", "max") or "max").strip().lower()
            if score_mode not in {"confidence", "peak", "max"}:
                errors.append("fusion.audio_fallback.score_mode must be one of: confidence, peak, max")
        interruption_cfg = fusion_cfg.get("interruption", {})
        if interruption_cfg is not None and not isinstance(interruption_cfg, dict):
            errors.append("fusion.interruption must be a mapping when provided")
            interruption_cfg = {}
        if isinstance(interruption_cfg, dict):
            for key in ("handoff_margin", "interrupt_min_delta", "score_smoothing_alpha"):
                if key in interruption_cfg:
                    try:
                        value = float(interruption_cfg.get(key))
                    except Exception:
                        errors.append(f"fusion.interruption.{key} must be numeric")
                        continue
                    if key == "score_smoothing_alpha":
                        if value < 0.0 or value > 1.0:
                            errors.append("fusion.interruption.score_smoothing_alpha must be in [0, 1]")
                    elif value < 0.0 or value > 1.0:
                        errors.append(f"fusion.interruption.{key} must be in [0, 1]")
            if "interrupt_hold_ms" in interruption_cfg:
                try:
                    value = float(interruption_cfg.get("interrupt_hold_ms"))
                except Exception:
                    errors.append("fusion.interruption.interrupt_hold_ms must be numeric")
                else:
                    if value <= 0.0:
                        errors.append("fusion.interruption.interrupt_hold_ms must be > 0")

    denoise_cfg = audio_cfg.get("denoise", {})
    if isinstance(denoise_cfg, dict):
        backend = str(denoise_cfg.get("backend", "wiener") or "wiener").lower()
        if backend not in {"wiener", "rnnoise", "hybrid"}:
            errors.append("audio.denoise.backend must be one of: wiener, rnnoise, hybrid")
        rnnoise_cfg = denoise_cfg.get("rnnoise", {})
        if rnnoise_cfg is not None and not isinstance(rnnoise_cfg, dict):
            errors.append("audio.denoise.rnnoise must be a dict when provided")
        elif isinstance(rnnoise_cfg, dict):
            model_path = rnnoise_cfg.get("model_path", "")
            if model_path is not None and not isinstance(model_path, str):
                errors.append("audio.denoise.rnnoise.model_path must be string")
        hybrid_cfg = denoise_cfg.get("hybrid", {})
        if hybrid_cfg is not None and not isinstance(hybrid_cfg, dict):
            errors.append("audio.denoise.hybrid must be a dict when provided")
        elif isinstance(hybrid_cfg, dict):
            postfilter_strength = float(hybrid_cfg.get("postfilter_strength", 0.5) or 0.5)
            if not 0.0 <= postfilter_strength <= 1.0:
                errors.append("audio.denoise.hybrid.postfilter_strength must be in [0, 1]")

    bench_cfg = config.get("bench", {})
    if not isinstance(bench_cfg, dict):
        bench_cfg = {}
    bench_targets = bench_cfg.get("targets", {})
    if not isinstance(bench_targets, dict):
        errors.append("bench.targets must be a mapping")
        bench_targets = {}
    for key in (
        "si_sdr_delta_db_min",
        "stoi_delta_min",
        "wer_relative_improvement_min",
        "sir_delta_db_min",
        "latency_p95_ms_max",
        "latency_p99_ms_max",
        "audio_queue_full_max",
        "audio_underrun_rate_max",
    ):
        if key not in bench_targets:
            continue
        try:
            value = float(bench_targets[key])
        except Exception:
            errors.append(f"bench.targets.{key} must be numeric")
            continue
        if value < 0:
            errors.append(f"bench.targets.{key} must be >= 0")

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

    uma8_cfg = config.get("uma8_leds", {})
    if uma8_cfg is not None and not isinstance(uma8_cfg, dict):
        errors.append("uma8_leds must be a mapping when provided")
        uma8_cfg = {}
    if isinstance(uma8_cfg, dict):
        if "strict_transport" in uma8_cfg and not isinstance(uma8_cfg.get("strict_transport"), bool):
            errors.append("uma8_leds.strict_transport must be bool")
        backend = str(uma8_cfg.get("backend", "simulate") or "simulate").strip().lower()
        if backend not in {"hid", "simulate", "none"}:
            errors.append("uma8_leds.backend must be one of: hid, simulate, none")

        try:
            ring_size = int(uma8_cfg.get("ring_size", 12))
            if ring_size < 1:
                errors.append("uma8_leds.ring_size must be >= 1")
        except Exception:
            errors.append("uma8_leds.ring_size must be integer")

        try:
            update_hz = float(uma8_cfg.get("update_hz", 12.0))
            if update_hz <= 0.0:
                errors.append("uma8_leds.update_hz must be > 0")
        except Exception:
            errors.append("uma8_leds.update_hz must be numeric")

        try:
            _ = float(uma8_cfg.get("base_bearing_offset_deg", 0.0))
        except Exception:
            errors.append("uma8_leds.base_bearing_offset_deg must be numeric")

        try:
            smoothing_alpha = float(uma8_cfg.get("smoothing_alpha", 0.35))
            if not 0.0 <= smoothing_alpha <= 1.0:
                errors.append("uma8_leds.smoothing_alpha must be in [0, 1]")
        except Exception:
            errors.append("uma8_leds.smoothing_alpha must be numeric")

        try:
            brightness_min = float(uma8_cfg.get("brightness_min", 0.05))
            brightness_max = float(uma8_cfg.get("brightness_max", 0.85))
            if not 0.0 <= brightness_min <= 1.0:
                errors.append("uma8_leds.brightness_min must be in [0, 1]")
            if not 0.0 <= brightness_max <= 1.0:
                errors.append("uma8_leds.brightness_max must be in [0, 1]")
            if brightness_max < brightness_min:
                errors.append("uma8_leds.brightness_max must be >= brightness_min")
        except Exception:
            errors.append("uma8_leds.brightness_min/max must be numeric")

        for rgb_key in ("idle_rgb", "lock_rgb", "search_rgb"):
            if rgb_key in uma8_cfg and not _is_rgb_triplet(uma8_cfg.get(rgb_key)):
                errors.append(f"uma8_leds.{rgb_key} must be a 3-item RGB list in [0, 255]")

        for key in ("vendor_id", "product_id"):
            if key not in uma8_cfg:
                continue
            try:
                value = int(uma8_cfg.get(key, 0))
                if value <= 0 or value > 0xFFFF:
                    errors.append(f"uma8_leds.{key} must be in [1, 65535]")
            except Exception:
                errors.append(f"uma8_leds.{key} must be integer")

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


def _is_rgb_triplet(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    for item in value:
        try:
            v = int(item)
        except Exception:
            return False
        if v < 0 or v > 255:
            return False
    return True
