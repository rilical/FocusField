"""
CONTRACT: inline (source: src/focusfield/main/modes.md)
ROLE: Define runtime modes and mode metadata.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - runtime.mode: selected runtime mode
"""

from __future__ import annotations

import copy
from typing import Any, Dict


_MODE_ALIASES = {
    "vision": "mac_loopback_dev",
    "virtual": "mac_loopback_dev",
    "virtual_mic": "mac_loopback_dev",
}


_MODE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "meeting_peripheral": {
        "runtime": {
            "process_mode": "threaded",
            "fail_fast": False,
            "startup": {
                "audio_first": True,
                "vision_start_delay_ms": 350,
                "validate_runtime_models": True,
                "defer_ui_until_vision": True,
                "overload_shed_enabled": True,
            },
            "requirements": {
                "strict": False,
                "min_cameras": 0,
                "min_audio_channels": 1,
                "camera_scope": "any",
            },
        },
        "trace": {
            "enabled": False,
        },
        "ui": {
            "enabled": False,
            "telemetry_hz": 5.0,
            "frame_max_hz": 2.0,
        },
        "output": {
            "sink": "usb_mic",
        },
        "audio": {
            "block_size": 960,
            "capture": {
                "queue_depth": 24,
                "reconnect_delay_ms": 750,
            },
            "agc_post": {
                "enabled": True,
                "target_rms": 0.11,
                "max_gain": 5.0,
                "min_gain": 0.4,
                "attack_alpha": 0.28,
                "release_alpha": 0.92,
                "limiter_threshold": 0.92,
            },
            "models": {
                "allow_runtime_downloads": False,
            },
        },
        "vision": {
            "models": {
                "allow_runtime_downloads": False,
            },
            "face": {
                "detect_width": 416,
                "detect_every_n": 1,
            },
            "mouth": {
                "backend": "tflite",
                "use_facemesh": True,
                "mesh_every_n": 1,
                "tflite_min_presence": 0.35,
            },
        },
        "fusion": {
            "telemetry": {
                "top_k": 3,
            },
        },
        "bus": {
            "topic_queue_policies": {
                "audio.frames": "drop_oldest",
                "audio.enhanced.beamformed": "drop_oldest",
                "audio.enhanced.final": "drop_oldest",
                "vision.frames.*": "drop_oldest",
                "ui.telemetry": "drop_oldest",
            },
        },
    },
    "mac_loopback_dev": {
        "runtime": {
            "process_mode": "threaded",
            "fail_fast": False,
            "startup": {
                "audio_first": True,
                "vision_start_delay_ms": 120,
                "validate_runtime_models": False,
                "defer_ui_until_vision": False,
                "overload_shed_enabled": True,
            },
            "requirements": {
                "strict": False,
                "min_cameras": 0,
                "min_audio_channels": 1,
                "camera_scope": "any",
            },
        },
        "trace": {
            "enabled": False,
        },
        "ui": {
            "enabled": True,
            "telemetry_hz": 8.0,
            "frame_max_hz": 4.0,
        },
        "output": {
            "sink": "host_loopback",
        },
        "audio": {
            "agc_post": {
                "enabled": True,
                "target_rms": 0.1,
                "max_gain": 4.0,
                "min_gain": 0.4,
                "attack_alpha": 0.3,
                "release_alpha": 0.94,
                "limiter_threshold": 0.92,
            },
        },
        "vision": {
            "models": {
                "allow_runtime_downloads": True,
            },
            "mouth": {
                "backend": "auto",
                "use_facemesh": True,
                "mesh_every_n": 1,
                "tflite_min_presence": 0.2,
            },
        },
        "fusion": {
            "telemetry": {
                "top_k": 3,
            },
        },
    },
    "appliance_fastboot": {
        "runtime": {
            "process_mode": "threaded",
            "fail_fast": False,
            "startup": {
                "audio_first": True,
                "vision_start_delay_ms": 500,
                "validate_runtime_models": True,
                "defer_ui_until_vision": True,
                "overload_shed_enabled": True,
            },
            "requirements": {
                "strict": False,
                "min_cameras": 0,
                "min_audio_channels": 1,
                "camera_scope": "usb",
            },
        },
        "trace": {
            "enabled": False,
        },
        "ui": {
            "enabled": False,
            "telemetry_hz": 2.0,
            "frame_max_hz": 1.0,
        },
        "output": {
            "sink": "usb_mic",
        },
        "audio": {
            "capture": {
                "reconnect_delay_ms": 500,
            },
            "agc_post": {
                "enabled": True,
                "target_rms": 0.11,
                "max_gain": 5.0,
                "min_gain": 0.4,
                "attack_alpha": 0.28,
                "release_alpha": 0.92,
                "limiter_threshold": 0.92,
            },
            "models": {
                "allow_runtime_downloads": False,
            },
        },
        "vision": {
            "models": {
                "allow_runtime_downloads": False,
            },
            "mouth": {
                "backend": "tflite",
                "use_facemesh": True,
                "mesh_every_n": 2,
                "tflite_min_presence": 0.35,
            },
        },
        "fusion": {
            "telemetry": {
                "top_k": 2,
            },
        },
        "bus": {
            "topic_queue_policies": {
                "audio.frames": "drop_oldest",
                "audio.enhanced.beamformed": "drop_oldest",
                "audio.enhanced.final": "drop_oldest",
                "vision.frames.*": "drop_oldest",
                "ui.telemetry": "drop_oldest",
            },
        },
    },
    "bench": {
        "runtime": {
            "process_mode": "threaded",
            "startup": {
                "audio_first": False,
                "vision_start_delay_ms": 0,
                "validate_runtime_models": False,
                "defer_ui_until_vision": False,
                "overload_shed_enabled": False,
            },
        },
        "trace": {
            "enabled": True,
        },
        "ui": {
            "enabled": False,
        },
        "output": {
            "sink": "file",
        },
        "fusion": {
            "telemetry": {
                "top_k": 5,
            },
        },
    },
}


KNOWN_RUNTIME_MODES = tuple(sorted(_MODE_DEFAULTS.keys()))


def normalize_runtime_mode(mode: Any) -> str:
    value = str(mode or "mac_loopback_dev").strip().lower()
    value = _MODE_ALIASES.get(value, value)
    if value not in _MODE_DEFAULTS:
        raise ValueError(f"Unknown runtime mode: {mode}")
    return value


def apply_mode_defaults(config: Dict[str, Any]) -> str:
    runtime_cfg = config.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        config["runtime"] = runtime_cfg
    mode = normalize_runtime_mode(runtime_cfg.get("mode", "mac_loopback_dev"))
    runtime_cfg["mode"] = mode
    defaults = copy.deepcopy(_MODE_DEFAULTS[mode])
    _merge_missing(config, defaults)
    config.setdefault("runtime", {})["mode"] = mode
    return mode


def mode_config(mode: str) -> Dict[str, Any]:
    normalized = normalize_runtime_mode(mode)
    return copy.deepcopy(_MODE_DEFAULTS[normalized])


def _merge_missing(target: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    for key, value in defaults.items():
        if isinstance(value, dict):
            node = target.get(key)
            if not isinstance(node, dict):
                target[key] = copy.deepcopy(value)
                continue
            _merge_missing(node, value)
            continue
        if key not in target:
            target[key] = copy.deepcopy(value)
