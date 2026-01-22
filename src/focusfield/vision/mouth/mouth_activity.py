"""
CONTRACT: inline (source: src/focusfield/vision/mouth/mouth_activity.md)
ROLE: Mouth activity estimation.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - vision.mouth.smoothing_alpha: EMA smoothing

PERF / TIMING:
  - per-frame

FAILURE MODES:
  - missing landmarks -> log landmarks_missing

LOG EVENTS:
  - module=vision.mouth_activity, event=landmarks_missing, payload keys=track_id

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/mouth/mouth_activity.md):
# Mouth activity

## Definition

- mouth_activity is a stable scalar in [0, 1].

## Inputs

- Face landmarks or mouth ROI per track.

## Output

- mouth_activity per FaceTrack.

## Filtering

- Exponential smoothing with alpha defined in config.

## Speaking trigger rules

- mouth_activity > speak_on_threshold for N frames -> speaking.
- mouth_activity < speak_off_threshold for M frames -> not speaking.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
