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
from typing import Any, Dict

import yaml


def load_config(path: str) -> Dict[str, Any]:
    """Load YAML config and apply defaults."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    defaults = _default_config()
    merged = _merge_dicts(defaults, data)
    _apply_thresholds_preset(merged, path)
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
        "ui": {
            "host": "127.0.0.1",
            "port": 8080,
            "telemetry_hz": 15,
        },
        "vision": {
            "face": {
                "min_confidence": 0.6,
                "iou_threshold": 0.3,
                "max_missing_frames": 10,
                "min_area": 900,
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
        },
        "bus": {
            "max_queue_depth": 8,
        },
        "logging": {
            "level": "info",
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
