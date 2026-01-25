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

from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
try:
    import mediapipe.tasks as mp_tasks
    from mediapipe.tasks.python.vision.core.image import Image, ImageFormat
except ImportError:  # pragma: no cover
    mp_tasks = None
    Image = None
    ImageFormat = None
import urllib.request
from pathlib import Path


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
        return self.smooth(track_id, scaled)

    def smooth(self, track_id: str, value: float) -> float:
        previous = self._smoothed.get(track_id, 0.0)
        smoothed = self._alpha * value + (1.0 - self._alpha) * previous
        self._smoothed[track_id] = smoothed
        return smoothed

    def drop(self, track_id: str) -> None:
        self._prev_roi.pop(track_id, None)
        self._smoothed.pop(track_id, None)


class FaceMeshMouthEstimator:
    """MediaPipe Tasks FaceLandmarker-based mouth activity estimator."""

    def __init__(
        self,
        max_faces: int = 5,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        min_activity: float = 0.02,
        max_activity: float = 0.25,
        model_path: Optional[str] = None,
    ) -> None:
        if mp_tasks is None or Image is None or ImageFormat is None:
            raise RuntimeError("mediapipe tasks API is not available")
        model_path = _ensure_model_path(model_path)
        base_options = mp_tasks.BaseOptions(model_asset_path=model_path)
        options = mp_tasks.vision.FaceLandmarkerOptions(
            base_options=base_options,
            num_faces=max_faces,
            min_face_detection_confidence=min_detection_confidence,
            min_face_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._landmarker = mp_tasks.vision.FaceLandmarker.create_from_options(options)
        self._min_activity = min_activity
        self._max_activity = max_activity

    def estimate(self, frame_bgr: np.ndarray) -> List[Dict[str, Any]]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = Image(image_format=ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect(mp_image)
        if not result.face_landmarks:
            return []
        height, width = frame_bgr.shape[:2]
        outputs: List[Dict[str, Any]] = []
        for face_landmarks in result.face_landmarks:
            pts = [(int(lm.x * width), int(lm.y * height)) for lm in face_landmarks]
            x_coords = [p[0] for p in pts]
            y_coords = [p[1] for p in pts]
            x1, x2 = max(0, min(x_coords)), min(width - 1, max(x_coords))
            y1, y2 = max(0, min(y_coords)), min(height - 1, max(y_coords))
            bbox = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
            activity = _mouth_aspect_ratio(pts)
            scaled = _scale(activity, self._min_activity, self._max_activity)
            outputs.append({"bbox": bbox, "activity": scaled})
        return outputs


def _mouth_aspect_ratio(points: List[Tuple[int, int]]) -> float:
    if len(points) < 292:
        return 0.0
    upper = points[13]
    lower = points[14]
    left = points[61]
    right = points[291]
    vertical = _distance(upper, lower)
    horizontal = _distance(left, right)
    if horizontal <= 1e-6:
        return 0.0
    return vertical / horizontal


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


def _distance(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    dx = float(a[0] - b[0])
    dy = float(a[1] - b[1])
    return float(np.hypot(dx, dy))


def _ensure_model_path(model_path: Optional[str]) -> str:
    if model_path:
        path = Path(model_path)
    else:
        cache_dir = Path.home() / ".cache" / "focusfield"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "face_landmarker.task"
    if not path.exists():
        url = (
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
            "face_landmarker/float16/latest/face_landmarker.task"
        )
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to download FaceLandmarker model: {exc}") from exc
    return str(path)
