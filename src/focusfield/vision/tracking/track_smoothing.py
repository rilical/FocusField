"""
CONTRACT: inline (source: src/focusfield/vision/tracking/track_smoothing.md)
ROLE: Track persistence and smoothing.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - vision.track.smoothing_alpha: smoothing factor
  - vision.track.max_missing_frames: drop threshold

PERF / TIMING:
  - per-frame smoothing

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/tracking/track_smoothing.md):
# Track smoothing

- Define track persistence rules.
- Handle brief occlusions without ID churn.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
