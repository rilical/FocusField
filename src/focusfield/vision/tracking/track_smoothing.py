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

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


BBox = Tuple[int, int, int, int]


@dataclass
class TrackState:
    track_id: int
    bbox: BBox
    confidence: float
    missing_frames: int = 0
    age_frames: int = 1
    matched: bool = True
    smooth_bbox: Tuple[float, float, float, float] = field(default_factory=lambda: (0.0, 0.0, 0.0, 0.0))


class TrackSmoother:
    """Simple IOU-based tracker with smoothing."""

    def __init__(self, iou_threshold: float = 0.3, max_missing_frames: int = 10, smoothing_alpha: float = 0.6):
        self._iou_threshold = iou_threshold
        self._max_missing = max_missing_frames
        self._alpha = smoothing_alpha
        self._tracks: Dict[int, TrackState] = {}
        self._next_id = 1

    def update(self, detections: List[Tuple[BBox, float]]) -> List[TrackState]:
        """Assign detections to tracks and return active tracks."""
        assigned = set()
        detections_sorted = list(detections)
        for track in list(self._tracks.values()):
            track.matched = False
            best_iou = 0.0
            best_idx = -1
            for idx, (bbox, _) in enumerate(detections_sorted):
                if idx in assigned:
                    continue
                iou = _iou(track.bbox, bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_idx = idx
            if best_iou >= self._iou_threshold and best_idx >= 0:
                bbox, conf = detections_sorted[best_idx]
                assigned.add(best_idx)
                track.bbox = bbox
                track.confidence = conf
                track.missing_frames = 0
                track.age_frames += 1
                track.matched = True
                track.smooth_bbox = _smooth_bbox(track.smooth_bbox, bbox, self._alpha)
            else:
                track.missing_frames += 1

        for idx, (bbox, conf) in enumerate(detections_sorted):
            if idx in assigned:
                continue
            track_id = self._next_id
            self._next_id += 1
            state = TrackState(
                track_id=track_id,
                bbox=bbox,
                confidence=conf,
                smooth_bbox=_smooth_bbox(None, bbox, 1.0),
                age_frames=1,
                matched=True,
            )
            self._tracks[track_id] = state

        for track_id in list(self._tracks.keys()):
            if self._tracks[track_id].missing_frames > self._max_missing:
                del self._tracks[track_id]

        return list(self._tracks.values())


def _smooth_bbox(
    previous: Optional[Tuple[float, float, float, float]],
    bbox: BBox,
    alpha: float,
) -> Tuple[float, float, float, float]:
    x, y, w, h = bbox
    if previous is None or alpha >= 1.0:
        return float(x), float(y), float(w), float(h)
    px, py, pw, ph = previous
    return (
        alpha * x + (1 - alpha) * px,
        alpha * y + (1 - alpha) * py,
        alpha * w + (1 - alpha) * pw,
        alpha * h + (1 - alpha) * ph,
    )


def _iou(box_a: BBox, box_b: BBox) -> float:
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    a_x2 = ax + aw
    a_y2 = ay + ah
    b_x2 = bx + bw
    b_y2 = by + bh

    inter_x1 = max(ax, bx)
    inter_y1 = max(ay, by)
    inter_x2 = min(a_x2, b_x2)
    inter_y2 = min(a_y2, b_y2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = aw * ah
    area_b = bw * bh
    union = area_a + area_b - inter_area
    if union <= 0:
        return 0.0
    return inter_area / union
