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

from __future__ import annotations


class SpeakingHysteresis:
    """Per-track hysteresis for speaking detection."""

    def __init__(
        self,
        speak_on_threshold: float,
        speak_off_threshold: float,
        min_on_frames: int = 3,
        min_off_frames: int = 3,
    ) -> None:
        self._on = speak_on_threshold
        self._off = speak_off_threshold
        self._min_on = max(1, min_on_frames)
        self._min_off = max(1, min_off_frames)
        self._on_count = 0
        self._off_count = 0
        self._speaking = False

    def update(self, activity: float) -> bool:
        if activity >= self._on:
            self._on_count += 1
            self._off_count = 0
            if self._on_count >= self._min_on:
                self._speaking = True
        elif activity <= self._off:
            self._off_count += 1
            self._on_count = 0
            if self._off_count >= self._min_off:
                self._speaking = False
        else:
            self._on_count = 0
            self._off_count = 0
        return self._speaking
