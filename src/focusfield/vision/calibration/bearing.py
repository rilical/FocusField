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

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
