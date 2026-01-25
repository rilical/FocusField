"""
CONTRACT: inline (source: src/focusfield/vision/calibration/bearing.md)
ROLE: Pixel to global bearing mapping.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - video.cameras[].hfov_deg: HFOV
  - video.cameras[].yaw_offset_deg: yaw offset
  - vision.calibration.bearing_offset_deg: optional offset

PERF / TIMING:
  - per-face computation

FAILURE MODES:
  - missing calibration -> log calibration_missing

LOG EVENTS:
  - module=vision.calibration.bearing, event=calibration_missing, payload keys=camera_id

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/calibration/bearing.md):
# Pixel to bearing mapping

- Convert pixel coordinates to bearing_deg.
- Apply camera yaw offsets before wrapping.
- Store calibration artifacts for reuse.
"""

from __future__ import annotations

from typing import Tuple


BBox = Tuple[int, int, int, int]


def bearing_from_bbox(
    bbox: BBox,
    frame_width: int,
    hfov_deg: float,
    yaw_offset_deg: float,
    bearing_offset_deg: float = 0.0,
) -> float:
    """Convert bbox center x into global bearing degrees."""
    x, _, w, _ = bbox
    center_x = x + 0.5 * w
    if frame_width <= 0:
        return _wrap_deg(yaw_offset_deg + bearing_offset_deg)
    normalized = (center_x - (frame_width / 2.0)) / frame_width
    bearing = normalized * hfov_deg + yaw_offset_deg + bearing_offset_deg
    return _wrap_deg(bearing)


def _wrap_deg(angle_deg: float) -> float:
    wrapped = angle_deg % 360.0
    if wrapped < 0:
        wrapped += 360.0
    return wrapped
