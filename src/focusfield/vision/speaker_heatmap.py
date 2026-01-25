"""
CONTRACT: inline (source: src/focusfield/vision/speaker_heatmap.md)
ROLE: Visual activity heatmap from face tracks.

INPUTS:
  - Topic: vision.face_tracks  Type: FaceTrack[]
OUTPUTS:
  - Topic: vision.speaker_heatmap  Type: DoaHeatmap

CONFIG KEYS:
  - vision.heatmap.bin_size_deg: bin size in degrees
  - vision.heatmap.sigma_deg: Gaussian spread (degrees)
  - vision.heatmap.top_k_peaks: peak count
  - vision.heatmap.smoothing_alpha: temporal smoothing

PERF / TIMING:
  - per face_tracks update or configured rate

FAILURE MODES:
  - no faces -> low confidence heatmap

LOG EVENTS:
  - module=vision.speaker_heatmap, event=heatmap_empty, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/speaker_heatmap.md):
# Visual speaker heatmap

- Aggregate mouth_activity over global bearing into a 0..360 heatmap.
- Use a Gaussian kernel per face track; normalize scores to 0..1.
- Emit top-K peaks for downstream consumers.
"""

from __future__ import annotations

import math
import queue
import threading
from typing import Any, Dict, List

import numpy as np

from focusfield.core.clock import now_ns


class SpeakerHeatmap:
    """Generate a visual activity heatmap from face tracks."""

    def __init__(self, bin_size_deg: float, sigma_deg: float, top_k_peaks: int, smoothing_alpha: float) -> None:
        self._bin_size_deg = bin_size_deg
        self._bins = max(1, int(round(360.0 / bin_size_deg)))
        self._sigma_deg = sigma_deg
        self._top_k = max(1, top_k_peaks)
        self._alpha = smoothing_alpha
        self._prev = np.zeros(self._bins, dtype=np.float32)
        self._seq = 0

    def update(self, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
        scores = np.zeros(self._bins, dtype=np.float32)
        for track in tracks:
            angle = float(track.get("bearing_deg", 0.0))
            activity = float(track.get("mouth_activity", 0.0))
            confidence = float(track.get("confidence", 1.0))
            amp = max(0.0, min(1.0, activity * confidence))
            if amp <= 0.0:
                continue
            self._add_gaussian(scores, angle, amp)

        max_val = float(scores.max()) if scores.size else 0.0
        if max_val > 0.0:
            scores /= max_val
        scores = self._alpha * scores + (1.0 - self._alpha) * self._prev
        self._prev = scores
        peaks = self._top_peaks(scores)
        self._seq += 1
        confidence = float(max_val)
        return {
            "t_ns": now_ns(),
            "seq": self._seq,
            "bins": self._bins,
            "bin_size_deg": self._bin_size_deg,
            "heatmap": scores.tolist(),
            "peaks": peaks,
            "confidence": confidence,
        }

    def _add_gaussian(self, scores: np.ndarray, center_deg: float, amplitude: float) -> None:
        sigma = max(1e-3, self._sigma_deg)
        for idx in range(self._bins):
            bin_angle = idx * self._bin_size_deg
            delta = _wrap_angle(bin_angle - center_deg)
            weight = math.exp(-(delta * delta) / (2.0 * sigma * sigma))
            scores[idx] += amplitude * weight

    def _top_peaks(self, scores: np.ndarray) -> List[Dict[str, float]]:
        if scores.size == 0:
            return []
        indices = np.argsort(scores)[::-1][: self._top_k]
        peaks = []
        for idx in indices:
            peaks.append(
                {
                    "angle_deg": float(idx * self._bin_size_deg),
                    "score": float(scores[idx]),
                }
            )
        return peaks


def start_speaker_heatmap(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    heatmap_cfg = config.get("vision", {}).get("heatmap", {})
    heatmap = SpeakerHeatmap(
        bin_size_deg=float(heatmap_cfg.get("bin_size_deg", 5.0)),
        sigma_deg=float(heatmap_cfg.get("sigma_deg", 12.0)),
        top_k_peaks=int(heatmap_cfg.get("top_k_peaks", 3)),
        smoothing_alpha=float(heatmap_cfg.get("smoothing_alpha", 0.3)),
    )
    q = bus.subscribe("vision.face_tracks")

    def _run() -> None:
        while not stop_event.is_set():
            try:
                tracks = q.get(timeout=0.1)
            except queue.Empty:
                continue
            msg = heatmap.update(tracks)
            if not tracks:
                logger.emit("debug", "vision.speaker_heatmap", "heatmap_empty", {"reason": "no_faces"})
            bus.publish("vision.speaker_heatmap", msg)

    thread = threading.Thread(target=_run, name="speaker-heatmap", daemon=True)
    thread.start()
    return thread


def _wrap_angle(angle_deg: float) -> float:
    angle = (angle_deg + 180.0) % 360.0 - 180.0
    return angle
