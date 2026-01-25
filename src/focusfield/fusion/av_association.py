"""
CONTRACT: inline (source: src/focusfield/fusion/av_association.md)
ROLE: Associate DOA peaks with face tracks.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.face_tracks  Type: FaceTrack[]
OUTPUTS:
  - Topic: fusion.candidates  Type: AssociationCandidate[]

CONFIG KEYS:
  - fusion.max_assoc_deg: max angular distance
  - fusion.score_weights: component weights

PERF / TIMING:
  - per heatmap update

FAILURE MODES:
  - no candidates -> emit empty list -> log no_candidates

LOG EVENTS:
  - module=fusion.av_association, event=no_candidates, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/fusion/av_association.md):
# AV association

- Match DOA peaks to face tracks by angular distance.
- Produce candidate list with confidence scores.
- Support configurable angular gating.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional, Tuple

from focusfield.core.clock import now_ns
from focusfield.fusion.confidence import combine_scores


def start_av_association(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    q_faces = bus.subscribe("vision.face_tracks")
    q_doa = bus.subscribe("audio.doa_heatmap")
    max_assoc_deg = float(config.get("fusion", {}).get("max_assoc_deg", 20.0))
    weights = config.get("fusion", {}).get("weights", {})
    last_doa: Optional[Dict[str, Any]] = None

    def _run() -> None:
        nonlocal last_doa
        while not stop_event.is_set():
            try:
                while True:
                    last_doa = q_doa.get_nowait()
            except queue.Empty:
                pass
            try:
                tracks = q_faces.get(timeout=0.1)
            except queue.Empty:
                continue
            candidates = _build_candidates(tracks, last_doa, max_assoc_deg, weights)
            if not candidates:
                logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "no_faces"})
            bus.publish("fusion.candidates", candidates)

    thread = threading.Thread(target=_run, name="av-association", daemon=True)
    thread.start()
    return thread


def _build_candidates(
    tracks: List[Dict[str, Any]],
    doa_heatmap: Optional[Dict[str, Any]],
    max_assoc_deg: float,
    weights: Dict[str, float],
) -> List[Dict[str, Any]]:
    peaks = doa_heatmap.get("peaks", []) if doa_heatmap else []
    candidates: List[Dict[str, Any]] = []
    for track in tracks:
        face_bearing = float(track.get("bearing_deg", 0.0))
        mouth_activity = float(track.get("mouth_activity", 0.0))
        face_confidence = float(track.get("confidence", 1.0))
        doa_peak_deg, doa_peak_score, angle_error = _match_peak(face_bearing, peaks, max_assoc_deg)
        combined = combine_scores(
            mouth_activity=mouth_activity,
            face_confidence=face_confidence,
            doa_peak_score=doa_peak_score,
            angle_error_deg=angle_error,
            weights=weights,
        )
        candidates.append(
            {
                "t_ns": now_ns(),
                "seq": track.get("seq", 0),
                "track_id": track.get("track_id"),
                "doa_peak_deg": doa_peak_deg,
                "angular_distance_deg": angle_error,
                "score_components": {
                    "mouth_activity": mouth_activity,
                    "face_confidence": face_confidence,
                    "doa_peak_score": doa_peak_score,
                },
                "combined_score": combined,
                "bearing_deg": face_bearing,
                "speaking": bool(track.get("speaking", False)),
            }
        )
    return candidates


def _match_peak(
    bearing_deg: float,
    peaks: List[Dict[str, Any]],
    max_assoc_deg: float,
) -> Tuple[Optional[float], float, float]:
    if not peaks:
        return None, 0.0, 180.0
    best_peak = None
    best_score = 0.0
    best_error = 180.0
    for peak in peaks:
        peak_angle = float(peak.get("angle_deg", 0.0))
        error = _wrap_delta(bearing_deg - peak_angle)
        if abs(error) < abs(best_error):
            best_error = error
            best_peak = peak_angle
            best_score = float(peak.get("score", 0.0))
    if abs(best_error) > max_assoc_deg:
        return None, 0.0, abs(best_error)
    return best_peak, best_score, abs(best_error)


def _wrap_delta(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0
