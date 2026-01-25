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

from __future__ import annotations

from typing import Dict, Optional, Tuple

import cv2
import numpy as np


BBox = Tuple[int, int, int, int]


class MouthActivityEstimator:
    """Estimate mouth activity from frame differencing in mouth ROI."""

    def __init__(
        self,
        smoothing_alpha: float,
        min_activity: float,
        max_activity: float,
        diff_threshold: float = 12.0,
    ) -> None:
        self._alpha = smoothing_alpha
        self._min_activity = min_activity
        self._max_activity = max_activity
        self._diff_threshold = diff_threshold
        self._prev_roi: Dict[str, np.ndarray] = {}
        self._smoothed: Dict[str, float] = {}

    def compute(self, track_id: str, frame_bgr: np.ndarray, bbox: BBox) -> float:
        roi = _extract_mouth_roi(frame_bgr, bbox)
        if roi is None:
            return 0.0
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        roi_gray = cv2.GaussianBlur(roi_gray, (3, 3), 0)
        prev = self._prev_roi.get(track_id)
        if prev is None or prev.shape != roi_gray.shape:
            raw = 0.0
        else:
            diff = cv2.absdiff(roi_gray, prev)
            if self._diff_threshold > 0:
                diff = np.where(diff >= self._diff_threshold, diff, 0)
            raw = float(np.mean(diff)) / 255.0
        self._prev_roi[track_id] = roi_gray
        scaled = _scale(raw, self._min_activity, self._max_activity)
        previous = self._smoothed.get(track_id, 0.0)
        smoothed = self._alpha * scaled + (1.0 - self._alpha) * previous
        self._smoothed[track_id] = smoothed
        return smoothed

    def drop(self, track_id: str) -> None:
        self._prev_roi.pop(track_id, None)
        self._smoothed.pop(track_id, None)


def _extract_mouth_roi(frame: np.ndarray, bbox: BBox) -> Optional[np.ndarray]:
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return None
    x1 = max(0, x + int(0.15 * w))
    x2 = max(0, x + int(0.85 * w))
    y1 = max(0, y + int(0.55 * h))
    y2 = max(0, y + int(0.9 * h))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def _scale(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    scaled = (value - min_value) / (max_value - min_value)
    if scaled < 0.0:
        return 0.0
    if scaled > 1.0:
        return 1.0
    return scaled
