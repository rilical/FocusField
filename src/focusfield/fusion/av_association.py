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
    min_peak_score = float(fallback_cfg.get("min_peak_score", 0.22))
    score_mode = str(fallback_cfg.get("score_mode", "max") or "max").strip().lower()
    require_vad = bool(fallback_cfg.get("require_vad", False))
    allow_when_faces_missing = bool(fallback_cfg.get("allow_when_faces_missing", True))
    face_staleness_ms = float(fallback_cfg.get("face_staleness_ms", 1200.0))
    vad_max_age_ms = float(fusion_cfg.get("vad_max_age_ms", 500.0))
    interruption_cfg = fusion_cfg.get("interruption", {})
    if not isinstance(interruption_cfg, dict):
        interruption_cfg = {}
    interrupt_min_delta = float(interruption_cfg.get("interrupt_min_delta", 0.04))
    ema_alpha = float(interruption_cfg.get("score_smoothing_alpha", 0.45))
    ema_alpha = max(0.0, min(1.0, ema_alpha))

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
    score_ema_by_track: Dict[str, float] = {}

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
                candidates = _build_candidates(
                    last_faces or [],
                    None,
                    max_assoc_deg,
                    weights,
                    min_area,
                    area_soft_max,
                    score_ema_by_track,
                    ema_alpha=ema_alpha,
                    interrupt_min_delta=interrupt_min_delta,
                )
                if not candidates:
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "faces_only"})
                bus.publish("fusion.candidates", candidates)
                continue

            # If DOA is healthy, only publish on DOA updates to keep cadence stable.
            if trigger != "doa":
                continue

            if faces_present and faces_fresh:
                candidates = _build_candidates(
                    last_faces or [],
                    last_doa,
                    max_assoc_deg,
                    weights,
                    min_area,
                    area_soft_max,
                    score_ema_by_track,
                    ema_alpha=ema_alpha,
                    interrupt_min_delta=interrupt_min_delta,
                )
                if not candidates:
                    reason = _no_assoc_reason(last_faces or [], last_doa, max_assoc_deg)
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": reason})
                bus.publish("fusion.candidates", candidates)
                continue

            # Vision missing/stale: optionally publish an audio-only candidate.
            candidates: List[Dict[str, Any]] = []
            if fallback_enabled and allow_when_faces_missing:
                audio_cand = _build_audio_only_candidate(
                    last_doa,
                    last_vad,
                    min_doa_confidence=min_doa_confidence,
                    min_peak_score=min_peak_score,
                    score_mode=score_mode,
                    require_vad=require_vad,
                    vad_max_age_ms=vad_max_age_ms,
                )
                if audio_cand is not None:
                    track_id = str(audio_cand.get("track_id", "audio:peak0"))
                    raw_score = float(audio_cand.get("raw_score", audio_cand.get("combined_score", 0.0)) or 0.0)
                    prev = score_ema_by_track.get(track_id, raw_score)
                    smoothed = ema_alpha * raw_score + (1.0 - ema_alpha) * prev
                    score_ema_by_track[track_id] = smoothed
                    audio_cand["smoothed_score"] = float(max(0.0, min(1.0, smoothed)))
                    audio_cand["combined_score"] = audio_cand["smoothed_score"]
                    candidates = [audio_cand]
            if not candidates:
                doa_conf = 0.0
                doa_peak = 0.0
                if isinstance(last_doa, dict):
                    doa_conf = float(last_doa.get("confidence", 0.0) or 0.0)
                    peaks = last_doa.get("peaks") or []
                    if isinstance(peaks, list) and peaks and isinstance(peaks[0], dict):
                        doa_peak = float(peaks[0].get("score", 0.0) or 0.0)
                low_conf_reason = "audio_low_confidence"
                if faces_present and faces_fresh:
                    low_conf_reason = _no_assoc_reason(last_faces or [], last_doa, max_assoc_deg)
                logger.emit(
                    "debug",
                    "fusion.av_association",
                    "no_candidates",
                    {
                        "reason": low_conf_reason,
                        "faces_fresh": bool(faces_fresh),
                        "faces_present": bool(faces_present),
                        "vad_speech": bool((last_vad or {}).get("speech")),
                        "doa_confidence": doa_conf,
                        "doa_peak_score": doa_peak,
                        "score_mode": score_mode,
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
    score_ema_by_track: Dict[str, float],
    ema_alpha: float,
    interrupt_min_delta: float,
) -> List[Dict[str, Any]]:
    peaks = doa_heatmap.get("peaks", []) if doa_heatmap else []
    candidates: List[Dict[str, Any]] = []
    for track in tracks:
        face_bearing = float(track.get("bearing_deg", 0.0))
        size_scale, bbox_area = _size_scale_for_track(track, min_area=min_area, area_soft_max=area_soft_max)
        mouth_activity_raw = float(track.get("mouth_activity", 0.0))
        face_confidence_raw = float(track.get("confidence", 1.0))
        facing_score = float(track.get("facing_score", 1.0) or 1.0)
        mouth_activity = mouth_activity_raw * size_scale
        face_confidence = face_confidence_raw * size_scale
        doa_peak_deg, doa_peak_score, angle_error = _match_peak(face_bearing, peaks, max_assoc_deg)
        doa_alignment = 0.0
        if doa_peak_deg is not None and max_assoc_deg > 0:
            doa_alignment = max(0.0, min(1.0, 1.0 - (float(angle_error) / float(max_assoc_deg))))
        if facing_score <= 0.05:
            continue
        has_visual_evidence = (mouth_activity > 0.0) or (face_confidence > 0.0)
        interrupt_bonus = float(
            interrupt_min_delta
            if bool(track.get("speaking", False)) and has_visual_evidence
            else 0.0
        )
        combined = combine_scores(
            mouth_activity=mouth_activity,
            face_confidence=face_confidence,
            doa_peak_score=doa_peak_score,
            angle_error_deg=angle_error,
            weights=weights,
            doa_alignment=doa_alignment,
            facing_score=facing_score,
            interrupt_bonus=interrupt_bonus,
        )
        track_id = str(track.get("track_id", ""))
        prev = score_ema_by_track.get(track_id, combined)
        smoothed = ema_alpha * combined + (1.0 - ema_alpha) * prev
        smoothed = float(max(0.0, min(1.0, smoothed)))
        score_ema_by_track[track_id] = smoothed
        mode = "AV_LOCK" if doa_peak_deg is not None else "VISION_ONLY"
        candidates.append(
            {
                "t_ns": now_ns(),
                "seq": track.get("seq", 0),
                "track_id": track.get("track_id"),
                "mode": mode,
                "doa_peak_deg": doa_peak_deg,
                "angular_distance_deg": angle_error,
                "score_components": {
                    "mouth_activity": mouth_activity,
                    "face_confidence": face_confidence,
                    "doa_peak_score": doa_peak_score,
                    "doa_alignment": float(doa_alignment),
                    "facing_score": float(facing_score),
                    "interrupt_bonus": float(interrupt_bonus),
                    "size_scale": float(size_scale),
                    "bbox_area": int(bbox_area),
                },
                "raw_score": float(combined),
                "smoothed_score": float(smoothed),
                "combined_score": float(smoothed),
                "bearing_deg": face_bearing,
                "speaking": bool(track.get("speaking", False)),
            }
        )
    _prune_score_ema(score_ema_by_track, candidates)
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
    min_peak_score: float,
    score_mode: str,
    require_vad: bool,
    vad_max_age_ms: float,
) -> Optional[Dict[str, Any]]:
    if doa_heatmap is None:
        return None
    vad_fresh = _vad_is_fresh(vad_state, vad_max_age_ms)
    vad_speaking = bool(vad_fresh and vad_state is not None and bool(vad_state.get("speech")))
    if require_vad and not vad_speaking:
        return None
    conf = float(doa_heatmap.get("confidence", 0.0) or 0.0)
    peaks = doa_heatmap.get("peaks") or []
    if not isinstance(peaks, list) or not peaks:
        return None
    peak0 = peaks[0] if isinstance(peaks[0], dict) else {}
    peak_score = float(peak0.get("score", 0.0) or 0.0)
    angle = peak0.get("angle_deg")
    if angle is None:
        return None

    mode = str(score_mode or "max").strip().lower()
    if mode == "confidence":
        gate_ok = conf >= float(min_doa_confidence)
        combined = conf
    elif mode == "peak":
        gate_ok = peak_score >= float(min_peak_score)
        combined = peak_score
    else:
        gate_ok = (conf >= float(min_doa_confidence)) or (peak_score >= float(min_peak_score))
        combined = max(conf, peak_score)
    if not gate_ok:
        return None

    bearing = float(angle) % 360.0
    combined = max(0.0, min(1.0, combined))
    return {
        "t_ns": now_ns(),
        "seq": int(doa_heatmap.get("seq", 0) or 0),
        "track_id": "audio:peak0",
        "mode": "AUDIO_ONLY",
        "doa_peak_deg": bearing,
        "angular_distance_deg": 0.0,
        "score_components": {
            "doa_peak_score": peak_score,
            "vad_confidence": float((vad_state or {}).get("confidence", 0.0) or 0.0),
            "doa_confidence": float(conf),
            "doa_alignment": 1.0,
            "facing_score": 0.0,
            "interrupt_bonus": 0.0,
            "score_mode": mode,
        },
        "raw_score": float(combined),
        "smoothed_score": float(combined),
        "combined_score": float(combined),
        "bearing_deg": bearing,
        "speaking": bool(vad_speaking),
    }


def _vad_is_fresh(vad_state: Optional[Dict[str, Any]], max_age_ms: float) -> bool:
    if vad_state is None:
        return False
    vad_t_ns = vad_state.get("t_ns")
    if vad_t_ns is None:
        return False
    try:
        return (now_ns() - int(vad_t_ns)) <= int(float(max_age_ms) * 1_000_000.0)
    except Exception:
        return False


def _prune_score_ema(score_ema_by_track: Dict[str, float], candidates: List[Dict[str, Any]]) -> None:
    active_ids = {str(c.get("track_id", "")) for c in candidates if c.get("track_id") is not None}
    if not active_ids:
        return
    for track_id in list(score_ema_by_track.keys()):
        if track_id not in active_ids and track_id.startswith("cam"):
            score_ema_by_track.pop(track_id, None)


def _no_assoc_reason(
    tracks: List[Dict[str, Any]],
    doa_heatmap: Optional[Dict[str, Any]],
    max_assoc_deg: float,
) -> str:
    if not tracks:
        return "no_faces_audio_fallback"
    if all(float(track.get("facing_score", 1.0) or 1.0) <= 0.05 for track in tracks):
        return "off_angle_face_decay"
    peaks = doa_heatmap.get("peaks", []) if isinstance(doa_heatmap, dict) else []
    if not peaks:
        return "audio_low_confidence"
    if max_assoc_deg <= 0:
        return "angle_mismatch"
    angles = []
    for peak in peaks:
        if isinstance(peak, dict):
            angle = peak.get("angle_deg")
            if angle is not None:
                angles.append(float(angle))
    if not angles:
        return "audio_low_confidence"
    for track in tracks:
        track_bearing = float(track.get("bearing_deg", 0.0) or 0.0)
        for angle in angles:
            err = abs(_wrap_delta(track_bearing - angle))
            if err <= max_assoc_deg:
                return "no_assoc"
    return "angle_mismatch"


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
