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
  - fusion.weights: component weights
  - fusion.audio_fallback.enabled: enable audio-only fallback when faces are missing/stale
  - fusion.audio_fallback.min_doa_confidence: minimum DOA confidence to steer on audio-only
  - fusion.audio_fallback.face_staleness_ms: face age threshold before considering vision stale
  - vision.face.min_area: minimum face area (also used for distance relevance weighting)
  - vision.face.area_soft_max: face area at which relevance saturates to 1.0

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
    q_vad = bus.subscribe("audio.vad")
    max_assoc_deg = float(config.get("fusion", {}).get("max_assoc_deg", 20.0))
    weights = config.get("fusion", {}).get("weights", {})
    fusion_cfg = config.get("fusion", {})
    if not isinstance(fusion_cfg, dict):
        fusion_cfg = {}
    fallback_cfg = fusion_cfg.get("audio_fallback", {})
    if not isinstance(fallback_cfg, dict):
        fallback_cfg = {}
    fallback_enabled = bool(fallback_cfg.get("enabled", True))
    min_doa_confidence = float(fallback_cfg.get("min_doa_confidence", 0.35))
    face_staleness_ms = float(fallback_cfg.get("face_staleness_ms", 1200.0))

    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    face_cfg = vision_cfg.get("face", {})
    if not isinstance(face_cfg, dict):
        face_cfg = {}
    min_area = int(face_cfg.get("min_area", 900))
    area_soft_max = int(face_cfg.get("area_soft_max", max(min_area * 4, min_area + 1)))

    last_faces: Optional[List[Dict[str, Any]]] = None
    last_faces_update_ns: int = 0
    last_doa: Optional[Dict[str, Any]] = None
    last_doa_update_ns: int = 0
    last_vad: Optional[Dict[str, Any]] = None

    def _run() -> None:
        nonlocal last_doa, last_doa_update_ns, last_faces, last_faces_update_ns, last_vad

        def _drain_latest(q: queue.Queue) -> Optional[Any]:
            item = None
            try:
                while True:
                    item = q.get_nowait()
            except queue.Empty:
                pass
            return item

        while not stop_event.is_set():
            # Publish is primarily driven by DOA cadence. If DOA is missing/stale,
            # fall back to face-driven updates so vision-only locking still works.
            trigger = None
            try:
                doa = q_doa.get(timeout=0.05)
                last_doa = doa
                last_doa_update_ns = now_ns()
                trigger = "doa"
            except queue.Empty:
                doa = None

            faces = _drain_latest(q_faces)
            if faces is not None:
                last_faces = faces
                last_faces_update_ns = now_ns()
                if trigger is None:
                    trigger = "faces"

            vad = _drain_latest(q_vad)
            if vad is not None:
                last_vad = vad

            if trigger is None:
                continue

            faces_fresh = bool(last_faces_update_ns) and (now_ns() - last_faces_update_ns) <= int(face_staleness_ms * 1_000_000)
            faces_present = bool(last_faces) and len(last_faces) > 0

            doa_fresh = bool(last_doa_update_ns) and (now_ns() - last_doa_update_ns) <= int(500.0 * 1_000_000)
            if trigger == "faces" and not doa_fresh:
                candidates = _build_candidates(last_faces or [], None, max_assoc_deg, weights, min_area, area_soft_max)
                if not candidates:
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "faces_only"})
                bus.publish("fusion.candidates", candidates)
                continue

            # If DOA is healthy, only publish on DOA updates to keep cadence stable.
            if trigger != "doa":
                continue

            if faces_present and faces_fresh:
                candidates = _build_candidates(last_faces or [], last_doa, max_assoc_deg, weights, min_area, area_soft_max)
                if not candidates:
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "no_assoc"})
                bus.publish("fusion.candidates", candidates)
                continue

            # Vision missing/stale: optionally publish an audio-only candidate.
            candidates: List[Dict[str, Any]] = []
            if fallback_enabled:
                audio_cand = _build_audio_only_candidate(last_doa, last_vad, min_doa_confidence)
                if audio_cand is not None:
                    candidates = [audio_cand]
            if not candidates:
                logger.emit(
                    "debug",
                    "fusion.av_association",
                    "no_candidates",
                    {
                        "reason": "no_faces_audio_fallback",
                        "faces_fresh": bool(faces_fresh),
                        "faces_present": bool(faces_present),
                    },
                )
            bus.publish("fusion.candidates", candidates)

    thread = threading.Thread(target=_run, name="av-association", daemon=True)
    thread.start()
    return thread


def _build_candidates(
    tracks: List[Dict[str, Any]],
    doa_heatmap: Optional[Dict[str, Any]],
    max_assoc_deg: float,
    weights: Dict[str, float],
    min_area: int,
    area_soft_max: int,
) -> List[Dict[str, Any]]:
    peaks = doa_heatmap.get("peaks", []) if doa_heatmap else []
    candidates: List[Dict[str, Any]] = []
    for track in tracks:
        face_bearing = float(track.get("bearing_deg", 0.0))
        size_scale, bbox_area = _size_scale_for_track(track, min_area=min_area, area_soft_max=area_soft_max)
        mouth_activity_raw = float(track.get("mouth_activity", 0.0))
        face_confidence_raw = float(track.get("confidence", 1.0))
        mouth_activity = mouth_activity_raw * size_scale
        face_confidence = face_confidence_raw * size_scale
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
                    "size_scale": float(size_scale),
                    "bbox_area": int(bbox_area),
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


def _build_audio_only_candidate(
    doa_heatmap: Optional[Dict[str, Any]],
    vad_state: Optional[Dict[str, Any]],
    min_doa_confidence: float,
) -> Optional[Dict[str, Any]]:
    if doa_heatmap is None:
        return None
    if vad_state is None or not bool(vad_state.get("speech")):
        return None
    conf = float(doa_heatmap.get("confidence", 0.0) or 0.0)
    if conf < float(min_doa_confidence):
        return None
    peaks = doa_heatmap.get("peaks") or []
    if not isinstance(peaks, list) or not peaks:
        return None
    peak0 = peaks[0] if isinstance(peaks[0], dict) else {}
    angle = peak0.get("angle_deg")
    if angle is None:
        return None
    doa_peak_score = float(peak0.get("score", 0.0))
    bearing = float(angle) % 360.0
    combined = max(0.0, min(1.0, conf))
    return {
        "t_ns": now_ns(),
        "seq": int(doa_heatmap.get("seq", 0) or 0),
        "track_id": "audio:peak0",
        "doa_peak_deg": bearing,
        "angular_distance_deg": 0.0,
        "score_components": {
            "doa_peak_score": doa_peak_score,
            "vad_confidence": float(vad_state.get("confidence", 0.0) or 0.0),
            "doa_confidence": float(conf),
        },
        "combined_score": float(combined),
        "bearing_deg": bearing,
        "speaking": True,
    }


def _size_scale_for_track(track: Dict[str, Any], min_area: int, area_soft_max: int) -> Tuple[float, int]:
    bbox = track.get("bbox") or {}
    if not isinstance(bbox, dict):
        bbox = {}
    w = int(bbox.get("w", 0) or 0)
    h = int(bbox.get("h", 0) or 0)
    area = int(max(0, w) * max(0, h))
    min_area = max(1, int(min_area))
    soft_max = max(int(area_soft_max), min_area + 1)
    if area <= min_area:
        return 0.0, area
    if area >= soft_max:
        return 1.0, area
    scale = (area - min_area) / float(soft_max - min_area)
    return float(max(0.0, min(1.0, scale))), area
