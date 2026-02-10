"""
CONTRACT: inline (source: src/focusfield/audio/doa/geometry.md)
ROLE: Array geometry helpers and steering vectors.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_profile: geometry source

PERF / TIMING:
  - precompute steering tables

FAILURE MODES:
  - invalid geometry -> raise -> log geometry_invalid

LOG EVENTS:
  - module=audio.doa.geometry, event=geometry_invalid, payload keys=reason

TESTS:
  - tests/contract_tests.md must cover geometry validation

CONTRACT DETAILS (inline from src/focusfield/audio/doa/geometry.md):
# Array geometry

- Supported geometry formats and units.
- Define steering vector assumptions.
- Validate geometry matches channel count.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Tuple

import yaml


MicPosition = Tuple[float, float]


def load_mic_positions(config: Dict[str, object]) -> Tuple[List[MicPosition], List[int]]:
    """Load mic positions (meters) and channel order from device_profiles.yaml."""
    audio_cfg = config.get("audio", {}) if isinstance(config, dict) else {}
    channels = int(audio_cfg.get("channels", 0) or 0)
    profile_name = str(audio_cfg.get("device_profile", "") or "")
    profiles = _load_device_profiles()
    mic_arrays = profiles.get("mic_arrays", {})
    profile = mic_arrays.get(profile_name)
    if not profile:
        raise ValueError(f"unknown mic profile: {profile_name}")

    geometry = str(profile.get("geometry", "circular")).lower()
    yaw_offset_deg = float(profile.get("yaw_offset_deg", 0.0) or 0.0)
    channel_order = profile.get("channel_order") or list(range(channels))
    if channels and len(channel_order) != channels:
        raise ValueError("channel_order length does not match channel count")

    if geometry == "circular":
        radius_m = float(profile.get("radius_m", 0.0))
        if radius_m <= 0:
            raise ValueError("radius_m must be > 0 for circular geometry")
        positions = _circular_positions(len(channel_order), radius_m)
        positions = _rotate_positions(positions, yaw_offset_deg)
        return positions, list(channel_order)

    if geometry == "custom":
        positions_raw = profile.get("positions_m")
        if not isinstance(positions_raw, list) or not positions_raw:
            raise ValueError("custom geometry requires positions_m list")
        positions: List[MicPosition] = [tuple(map(float, p)) for p in positions_raw]  # type: ignore[arg-type]
        if channels and len(positions) != channels:
            raise ValueError("positions_m length does not match channel count")
        positions = _rotate_positions(positions, yaw_offset_deg)
        return positions, list(channel_order)

    raise ValueError(f"unsupported geometry: {geometry}")


def _circular_positions(count: int, radius_m: float) -> List[MicPosition]:
    """Return evenly spaced mic positions on a ring.

    Positions are returned in *logical mic order* (index 0..count-1). Channel
    remapping is handled separately via `channel_order` when we reorder frames.
    """

    positions: List[MicPosition] = [(0.0, 0.0) for _ in range(count)]
    if count <= 0:
        return positions
    step = 2.0 * math.pi / float(count)
    for idx in range(count):
        angle = idx * step
        x = radius_m * math.cos(angle)
        y = radius_m * math.sin(angle)
        positions[idx] = (x, y)
    return positions


def _rotate_positions(positions: List[MicPosition], yaw_offset_deg: float) -> List[MicPosition]:
    if not positions:
        return positions
    if abs(yaw_offset_deg) <= 1e-9:
        return positions
    yaw_rad = math.radians(yaw_offset_deg)
    c = math.cos(yaw_rad)
    s = math.sin(yaw_rad)
    rotated: List[MicPosition] = []
    for (x, y) in positions:
        rotated.append((x * c - y * s, x * s + y * c))
    return rotated


def _load_device_profiles() -> Dict[str, Dict[str, object]]:
    config_path = Path(__file__).resolve().parents[4] / "configs" / "device_profiles.yaml"
    with open(config_path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}
