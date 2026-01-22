"""
CONTRACT: inline (source: src/focusfield/vision/mouth/thresholds.md)
ROLE: Speaking hysteresis from mouth activity.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - vision.mouth.speak_on_threshold: on threshold
  - vision.mouth.speak_off_threshold: off threshold
  - vision.mouth.min_on_frames: frames to confirm
  - vision.mouth.min_off_frames: frames to clear

PERF / TIMING:
  - per-frame

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/mouth/thresholds.md):
# Mouth activity thresholds

- speak_on_threshold and speak_off_threshold define hysteresis.
- Thresholds are defined per preset in configs.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
