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
  - video.cameras[].bearing_model: linear | lut
  - video.cameras[].bearing_lut_path: JSON list of pixel_x -> bearing_deg_cam
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

from typing import List, Optional, Tuple


BBox = Tuple[int, int, int, int]


def bearing_from_bbox(
    bbox: BBox,
    frame_width: int,
    hfov_deg: float,
    yaw_offset_deg: float,
    bearing_offset_deg: float = 0.0,
    bearing_model: str = "linear",
    bearing_lut: Optional[List[float]] = None,
) -> float:
    """Convert bbox center x into global bearing degrees."""
    x, _, w, _ = bbox
    center_x = x + 0.5 * w
    if frame_width <= 0:
        return _wrap_deg(yaw_offset_deg + bearing_offset_deg)
    model = (bearing_model or "linear").lower()
    if model == "lut" and bearing_lut:
        idx = _lut_index(center_x, frame_width, len(bearing_lut))
        bearing_cam = float(bearing_lut[idx])
        bearing = bearing_cam + yaw_offset_deg + bearing_offset_deg
        return _wrap_deg(bearing)
    normalized = (center_x - (frame_width / 2.0)) / frame_width
    bearing = normalized * hfov_deg + yaw_offset_deg + bearing_offset_deg
    return _wrap_deg(bearing)


def _wrap_deg(angle_deg: float) -> float:
    wrapped = angle_deg % 360.0
    if wrapped < 0:
        wrapped += 360.0
    return wrapped


def _lut_index(center_x: float, frame_width: int, lut_len: int) -> int:
    if lut_len <= 1 or frame_width <= 1:
        return 0
    ratio = center_x / float(frame_width - 1)
    idx = int(round(ratio * (lut_len - 1)))
    if idx < 0:
        return 0
    if idx >= lut_len:
        return lut_len - 1
    return idx
