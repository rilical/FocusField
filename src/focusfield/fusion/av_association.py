"""
CONTRACT: inline (source: src/focusfield/fusion/av_association.md)
ROLE: Associate DOA peaks with face tracks.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.face_tracks  Type: FaceTrack[]
OUTPUTS:
  - Topic: fusion.candidates  Type: FusionCandidatesEnvelope

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
from typing import Any, Dict, List, Optional, TypedDict, Tuple

from focusfield.core.clock import now_ns
from focusfield.fusion.confidence import combine_scores


class FusionCandidatesEnvelope(TypedDict):
    candidates: List[Dict[str, Any]]
    evidence: Dict[str, Any]


def start_av_association(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    q_faces = bus.subscribe("vision.face_tracks")
    q_doa = bus.subscribe("audio.doa_heatmap")
    q_vad = bus.subscribe("audio.vad")
    q_mic_health = bus.subscribe("audio.mic_health")
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
    vad_max_age_ms = float(fusion_cfg.get("vad_max_age_ms", fallback_cfg.get("vad_max_age_ms", 400.0)) or 400.0)
    visual_freshness_ms = float(fusion_cfg.get("visual_freshness_ms", face_staleness_ms) or face_staleness_ms)
    visual_override_min = float(fusion_cfg.get("visual_override_min", 0.6) or 0.6)
    audio_rescue_min = float(fusion_cfg.get("audio_rescue_min", min_doa_confidence) or min_doa_confidence)
    disagreement_penalty = float(fusion_cfg.get("disagreement_penalty", 0.25) or 0.25)

    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    face_cfg = vision_cfg.get("face", {})
    if not isinstance(face_cfg, dict):
        face_cfg = {}
    min_area = int(face_cfg.get("min_area", 900))
    area_soft_max = int(face_cfg.get("area_soft_max", max(min_area * 4, min_area + 1)))
    camera_lookup = _camera_lookup_from_config(config)

    last_faces: Optional[List[Dict[str, Any]]] = None
    last_faces_update_ns: int = 0
    last_doa: Optional[Dict[str, Any]] = None
    last_doa_update_ns: int = 0
    last_vad: Optional[Dict[str, Any]] = None
    last_mic_health: Optional[Dict[str, Any]] = None

    def _run() -> None:
        nonlocal last_doa, last_doa_update_ns, last_faces, last_faces_update_ns, last_vad, last_mic_health

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

            mic_health = _drain_latest(q_mic_health)
            if mic_health is not None:
                last_mic_health = mic_health

            if trigger is None:
                continue

            now_t_ns = now_ns()
            faces_fresh = bool(last_faces_update_ns) and (now_t_ns - last_faces_update_ns) <= int(visual_freshness_ms * 1_000_000)
            faces_present = bool(last_faces) and len(last_faces) > 0

            doa_fresh = bool(last_doa_update_ns) and (now_t_ns - last_doa_update_ns) <= int(500.0 * 1_000_000)
            vad_fresh = False
            if isinstance(last_vad, dict):
                vad_t_ns = int(last_vad.get("t_ns", 0) or 0)
                vad_fresh = bool(vad_t_ns) and ((now_t_ns - vad_t_ns) <= int(vad_max_age_ms * 1_000_000))
            if trigger == "faces" and not doa_fresh:
                candidates = _build_candidates(
                    last_faces or [],
                    None,
                    last_vad,
                    last_mic_health,
                    max_assoc_deg,
                    weights,
                    min_area,
                    area_soft_max,
                    camera_lookup=camera_lookup,
                    visual_override_min=visual_override_min,
                    disagreement_penalty=disagreement_penalty,
                )
                if not candidates:
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "faces_only"})
                bus.publish("fusion.candidates", _candidate_message(
                    candidates,
                    _candidate_evidence(
                        reason="faces_only",
                        faces_present=faces_present,
                        faces_fresh=faces_fresh,
                        doa_fresh=doa_fresh,
                        vad_fresh=vad_fresh,
                        vad_speech=bool((last_vad or {}).get("speech", False)),
                        candidates=candidates,
                    ),
                ))
                continue

            # If DOA is healthy, only publish on DOA updates to keep cadence stable.
            if trigger != "doa":
                continue

            if faces_present and faces_fresh:
                candidates = _build_candidates(
                    last_faces or [],
                    last_doa,
                    last_vad,
                    last_mic_health,
                    max_assoc_deg,
                    weights,
                    min_area,
                    area_soft_max,
                    camera_lookup=camera_lookup,
                    visual_override_min=visual_override_min,
                    disagreement_penalty=disagreement_penalty,
                )
                if not candidates:
                    logger.emit("debug", "fusion.av_association", "no_candidates", {"reason": "no_assoc"})
                bus.publish("fusion.candidates", _candidate_message(
                    candidates,
                    _candidate_evidence(
                        reason="no_assoc" if not candidates else "faces_and_audio",
                        faces_present=faces_present,
                        faces_fresh=faces_fresh,
                        doa_fresh=doa_fresh,
                        vad_fresh=vad_fresh,
                        vad_speech=bool((last_vad or {}).get("speech", False)),
                        candidates=candidates,
                    ),
                ))
                continue

            # Vision missing/stale: optionally publish an audio-only candidate.
            candidates: List[Dict[str, Any]] = []
            if fallback_enabled and allow_when_faces_missing:
                audio_cand = _build_audio_only_candidate(
                    last_doa,
                    last_vad,
                    last_mic_health,
                    min_doa_confidence=min_doa_confidence,
                    min_peak_score=min_peak_score,
                    score_mode=score_mode,
                    require_vad=require_vad,
                    weights=weights,
                    vad_max_age_ms=vad_max_age_ms,
                    audio_rescue_min=audio_rescue_min,
                )
                if audio_cand is not None:
                    candidates = [audio_cand]
            if not candidates:
                doa_conf = 0.0
                doa_peak = 0.0
                if isinstance(last_doa, dict):
                    doa_conf = float(last_doa.get("confidence", 0.0) or 0.0)
                    peaks = last_doa.get("peaks") or []
                    if isinstance(peaks, list) and peaks and isinstance(peaks[0], dict):
                        doa_peak = float(peaks[0].get("score", 0.0) or 0.0)
                logger.emit(
                    "debug",
                    "fusion.av_association",
                    "no_candidates",
                    {
                        "reason": "no_faces_audio_fallback",
                        "faces_fresh": bool(faces_fresh),
                        "faces_present": bool(faces_present),
                        "vad_speech": bool((last_vad or {}).get("speech")),
                        "doa_confidence": doa_conf,
                        "doa_peak_score": doa_peak,
                        "score_mode": score_mode,
                    },
                )
            bus.publish("fusion.candidates", _candidate_message(
                candidates,
                _candidate_evidence(
                    reason=_missing_visual_reason(faces_present, faces_fresh, candidates),
                    faces_present=faces_present,
                    faces_fresh=faces_fresh,
                    doa_fresh=doa_fresh,
                    vad_fresh=vad_fresh,
                    vad_speech=bool((last_vad or {}).get("speech", False)),
                    candidates=candidates,
                ),
            ))

    thread = threading.Thread(target=_run, name="av-association", daemon=True)
    thread.start()
    return thread


def _build_candidates(
    tracks: List[Dict[str, Any]],
    doa_heatmap: Optional[Dict[str, Any]],
    vad_state: Optional[Dict[str, Any]] = None,
    mic_health: Optional[Dict[str, Any]] = None,
    max_assoc_deg: float = 20.0,
    weights: Optional[Dict[str, float]] = None,
    min_area: int = 900,
    area_soft_max: int = 3600,
    camera_lookup: Optional[Any] = None,
    visual_override_min: float = 0.0,
    disagreement_penalty: float = 0.0,
) -> List[Dict[str, Any]]:
    peaks = doa_heatmap.get("peaks", []) if doa_heatmap else []
    doa_confidence = float(doa_heatmap.get("confidence", 0.0) or 0.0) if doa_heatmap else 0.0
    audio_speech_prob = _speech_probability(vad_state)
    mic_health_score, mic_health_trust = _mic_health_summary(mic_health)
    if weights is None:
        weights = {}
    camera_sectors = _normalize_camera_lookup(camera_lookup)
    candidates: List[Dict[str, Any]] = []
    for track in tracks:
        face_bearing = float(track.get("bearing_deg", 0.0))
        size_scale, bbox_area = _size_scale_for_track(track, min_area=min_area, area_soft_max=area_soft_max)
        mouth_activity_raw = float(track.get("visual_speaking_prob", track.get("mouth_activity", 0.0)))
        face_confidence_raw = float(track.get("confidence", 1.0))
        track_continuity = _track_continuity(track)
        mouth_activity = mouth_activity_raw * size_scale
        face_confidence = face_confidence_raw * size_scale
        doa_peak_deg, doa_peak_score, angle_error = _match_peak(face_bearing, peaks, max_assoc_deg)
        camera_id = _candidate_camera_id(track)
        steering_bearing_deg = _refine_steering_bearing(
            face_bearing,
            doa_peak_deg,
            doa_peak_score,
            camera_id,
            camera_sectors,
        )
        visual_score = _visual_score(mouth_activity, face_confidence)
        angle_match = _angle_match(angle_error)
        audio_alignment_score = _audio_alignment_score(doa_peak_score, doa_confidence, audio_speech_prob, angle_match)
        disagreement_gap = max(0.0, audio_alignment_score - visual_score)
        disagreement_cost = (
            float(disagreement_penalty) * disagreement_gap
            if visual_score < float(visual_override_min) and disagreement_gap > 0.0
            else 0.0
        )
        combined = combine_scores(
            mouth_activity=mouth_activity,
            face_confidence=face_confidence,
            doa_peak_score=doa_peak_score,
            doa_confidence=doa_confidence,
            angle_error_deg=angle_error,
            audio_speech_prob=audio_speech_prob,
            track_continuity=track_continuity,
            mic_health_score=mic_health_score,
            weights=weights,
        )
        if disagreement_cost > 0.0:
            combined = max(0.0, float(combined) - disagreement_cost)
        if visual_score >= float(visual_override_min) and mouth_activity > 0.0:
            combined = max(float(combined), visual_score)
        speaking_probability = _speaking_probability(
            visual_speaking_prob=mouth_activity,
            audio_speech_prob=audio_speech_prob,
            doa_peak_score=doa_peak_score,
            angle_error_deg=angle_error,
        )
        if size_scale <= 0.0 and doa_peak_score <= 0.0 and audio_speech_prob <= 0.0:
            combined = 0.0
            speaking_probability = 0.0
        selection_mode = "AUDIO_ONLY" if doa_peak_score > 0.0 and mouth_activity <= 0.0 else ("AV_LOCK" if doa_peak_score > 0.0 and mouth_activity > 0.0 else "VISION_ONLY")
        score_groups = {
            "visual_score": float(visual_score),
            "audio_alignment_score": float(audio_alignment_score),
            "continuity_score": float(track_continuity),
            "health_score": float(mic_health_score),
            "agreement_bonus": float(max(0.0, min(1.0, mouth_activity * doa_peak_score * audio_speech_prob))),
            "disagreement_penalty": float(disagreement_cost),
        }
        candidates.append(
            {
                "t_ns": now_ns(),
                "seq": track.get("seq", 0),
                "track_id": track.get("track_id"),
                "camera_id": camera_id,
                "doa_peak_deg": doa_peak_deg,
                "angular_distance_deg": angle_error,
                "score_components": {
                    "mouth_activity": mouth_activity,
                    "visual_speaking_prob": mouth_activity,
                    "face_confidence": face_confidence,
                    "doa_peak_score": doa_peak_score,
                    "doa_confidence": doa_confidence,
                    "audio_speech_prob": audio_speech_prob,
                    "track_continuity": track_continuity,
                    "mic_health_score": mic_health_score,
                    "mic_health_trust": mic_health_trust,
                    "size_scale": float(size_scale),
                    "bbox_area": int(bbox_area),
                    "angle_match": float(angle_match),
                    "disagreement_penalty_applied": float(disagreement_cost),
                },
                "score_groups": score_groups,
                "focus_score": combined,
                "combined_score": combined,
                "bearing_deg": face_bearing,
                "steering_bearing_deg": steering_bearing_deg,
                "activity_score": speaking_probability,
                "speaking_probability": speaking_probability,
                "selection_mode": selection_mode,
                "speaking": bool(track.get("speaking", False)) or speaking_probability >= 0.5,
                "evidence_status": {
                    "visual_fresh": True,
                    "audio_fresh": doa_heatmap is not None,
                    "disagreement_suppressed": bool(disagreement_cost > 0.0),
                },
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


def _refine_steering_bearing(
    face_bearing_deg: float,
    doa_peak_deg: Optional[float],
    doa_peak_score: float,
    camera_id: Optional[Any],
    camera_sectors: Dict[str, Dict[str, float]],
) -> float:
    if doa_peak_deg is None:
        return face_bearing_deg % 360.0
    if camera_id is None:
        return face_bearing_deg % 360.0
    sector = camera_sectors.get(str(camera_id))
    if not sector:
        return face_bearing_deg % 360.0
    yaw_offset_deg = float(sector.get("yaw_offset_deg", 0.0) or 0.0)
    hfov_deg = float(sector.get("hfov_deg", 0.0) or 0.0)
    if hfov_deg <= 0.0:
        return face_bearing_deg % 360.0
    if abs(_wrap_delta(doa_peak_deg - yaw_offset_deg)) > (hfov_deg / 2.0):
        return face_bearing_deg % 360.0
    blend = max(0.0, min(1.0, float(doa_peak_score or 0.0)))
    if blend <= 0.0:
        return face_bearing_deg % 360.0
    face = face_bearing_deg % 360.0
    delta = _wrap_delta((doa_peak_deg % 360.0) - face)
    return (face + (blend * delta)) % 360.0


def _candidate_camera_id(track: Dict[str, Any]) -> Optional[str]:
    raw = track.get("camera_id")
    if raw:
        return str(raw)
    track_id = str(track.get("track_id", "") or "")
    if track_id.startswith("cam") and "-" in track_id:
        return track_id.split("-", 1)[0]
    return None


def _wrap_delta(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0


def _build_audio_only_candidate(
    doa_heatmap: Optional[Dict[str, Any]],
    vad_state: Optional[Dict[str, Any]],
    mic_health: Optional[Dict[str, Any]],
    min_doa_confidence: float,
    min_peak_score: float,
    score_mode: str,
    require_vad: bool,
    weights: Dict[str, float],
    vad_max_age_ms: float = 400.0,
    audio_rescue_min: float = 0.0,
) -> Optional[Dict[str, Any]]:
    if doa_heatmap is None:
        return None
    vad_fresh = False
    vad_speaking = False
    if isinstance(vad_state, dict):
        vad_t_ns = int(vad_state.get("t_ns", 0) or 0)
        vad_fresh = bool(vad_t_ns) and ((now_ns() - vad_t_ns) / 1_000_000.0 <= float(vad_max_age_ms))
        vad_speaking = bool(vad_state.get("speech")) and vad_fresh
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
    audio_speech_prob = _speech_probability(vad_state)
    mic_health_score, mic_health_trust = _mic_health_summary(mic_health)
    if mode == "confidence":
        gate_ok = conf >= float(min_doa_confidence)
        evidence = conf
    elif mode == "peak":
        gate_ok = peak_score >= float(min_peak_score)
        evidence = peak_score
    else:
        gate_ok = (conf >= float(min_doa_confidence)) or (peak_score >= float(min_peak_score))
        evidence = max(conf, peak_score)
    if not gate_ok:
        return None

    bearing = float(angle) % 360.0
    combined = combine_scores(
        mouth_activity=0.0,
        face_confidence=0.0,
        doa_peak_score=peak_score,
        doa_confidence=conf,
        angle_error_deg=0.0,
        audio_speech_prob=audio_speech_prob,
        track_continuity=0.65,
        mic_health_score=mic_health_score,
        weights=weights,
    )
    if float(combined) < float(audio_rescue_min):
        return None
    angle_match = _angle_match(0.0)
    score_groups = {
        "visual_score": 0.0,
        "audio_alignment_score": float(_audio_alignment_score(peak_score, conf, audio_speech_prob, angle_match)),
        "continuity_score": 0.65,
        "health_score": float(mic_health_score),
        "agreement_bonus": 0.0,
        "disagreement_penalty": 0.0,
    }
    return {
        "t_ns": now_ns(),
        "seq": int(doa_heatmap.get("seq", 0) or 0),
        "track_id": "audio:peak0",
        "doa_peak_deg": bearing,
        "angular_distance_deg": 0.0,
        "score_components": {
            "doa_peak_score": peak_score,
            "vad_confidence": float((vad_state or {}).get("confidence", 0.0) or 0.0),
            "doa_confidence": float(conf),
            "audio_speech_prob": float(audio_speech_prob),
            "mic_health_score": float(mic_health_score),
            "mic_health_trust": float(mic_health_trust),
            "fallback_evidence": float(evidence),
            "score_mode": mode,
            "angle_match": float(angle_match),
        },
        "score_groups": score_groups,
        "focus_score": float(combined),
        "combined_score": float(combined),
        "bearing_deg": bearing,
        "steering_bearing_deg": bearing,
        "activity_score": float(max(audio_speech_prob, peak_score)),
        "speaking_probability": float(max(audio_speech_prob, peak_score)),
        "selection_mode": "AUDIO_ONLY",
        "speaking": bool(vad_speaking),
        "evidence_status": {
            "visual_fresh": False,
            "audio_fresh": True,
            "disagreement_suppressed": False,
        },
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


def _speech_probability(vad_state: Optional[Dict[str, Any]]) -> float:
    if vad_state is None:
        return 0.0
    confidence = float(vad_state.get("speech_probability", vad_state.get("confidence", 0.0)) or 0.0)
    return float(max(0.0, min(1.0, confidence)))


def _mic_health_summary(mic_health: Optional[Dict[str, Any]]) -> Tuple[float, float]:
    if not isinstance(mic_health, dict):
        return 1.0, 0.5
    mean_score = float(mic_health.get("mean_score", 1.0) or 1.0)
    mean_trust = float(mic_health.get("mean_trust", 0.5) or 0.5)
    return float(max(0.0, min(1.0, mean_score))), float(max(0.0, min(1.0, mean_trust)))


def _track_continuity(track: Dict[str, Any]) -> float:
    age_frames = float(track.get("track_age_frames", 0.0) or 0.0)
    return float(max(0.05, min(1.0, age_frames / 6.0)))


def _camera_lookup_from_config(config: Dict[str, Any]) -> Optional[Dict[str, Dict[str, float]]]:
    video_cfg = config.get("video", {})
    if not isinstance(video_cfg, dict):
        return None
    cameras = video_cfg.get("cameras", [])
    return _normalize_camera_lookup(cameras)


def _normalize_camera_lookup(camera_lookup: Optional[Any]) -> Dict[str, Dict[str, float]]:
    if camera_lookup is None:
        return {}
    if isinstance(camera_lookup, dict):
        items = camera_lookup.items()
    elif isinstance(camera_lookup, list):
        items = enumerate(camera_lookup)
    else:
        return {}
    normalized: Dict[str, Dict[str, float]] = {}
    for key, cam in items:
        if isinstance(cam, dict):
            camera_id = str(cam.get("id", key))
            yaw_offset_deg = float(cam.get("yaw_offset_deg", 0.0) or 0.0)
            hfov_deg = float(cam.get("hfov_deg", 0.0) or 0.0)
        else:
            continue
        normalized[camera_id] = {
            "yaw_offset_deg": yaw_offset_deg,
            "hfov_deg": hfov_deg,
        }
    return normalized


def _speaking_probability(
    visual_speaking_prob: float,
    audio_speech_prob: float,
    doa_peak_score: float,
    angle_error_deg: float,
) -> float:
    angle_match = max(0.0, min(1.0, 1.0 - (angle_error_deg / 90.0)))
    agreement = max(0.0, min(1.0, doa_peak_score * angle_match))
    score = 0.55 * visual_speaking_prob + 0.30 * audio_speech_prob + 0.15 * agreement
    return float(max(0.0, min(1.0, score)))


def _visual_score(mouth_activity: float, face_confidence: float) -> float:
    return float(max(0.0, min(1.0, (0.75 * float(mouth_activity)) + (0.25 * float(face_confidence)))))


def _angle_match(angle_error_deg: float) -> float:
    return float(max(0.0, min(1.0, 1.0 - (float(angle_error_deg) / 90.0))))


def _audio_alignment_score(doa_peak_score: float, doa_confidence: float, audio_speech_prob: float, angle_match: float) -> float:
    score = (0.40 * float(doa_peak_score)) + (0.25 * float(doa_confidence)) + (0.20 * float(audio_speech_prob)) + (0.15 * float(angle_match))
    return float(max(0.0, min(1.0, score)))


def _candidate_evidence(
    *,
    reason: str,
    faces_present: bool,
    faces_fresh: bool,
    doa_fresh: bool,
    vad_fresh: bool,
    vad_speech: bool,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "reason": str(reason),
        "faces_present": bool(faces_present),
        "faces_fresh": bool(faces_fresh),
        "visual_stale": bool(faces_present and not faces_fresh),
        "audio_fresh": bool(doa_fresh or vad_fresh),
        "audio_stale": not bool(doa_fresh or vad_fresh),
        "doa_fresh": bool(doa_fresh),
        "vad_fresh": bool(vad_fresh),
        "vad_speech": bool(vad_speech),
        "disagreement_suppressed": any(
            bool(((cand.get("score_groups") or {}).get("disagreement_penalty", 0.0)) > 0.0)
            for cand in candidates
            if isinstance(cand, dict)
        ),
    }


def _candidate_message(candidates: List[Dict[str, Any]], evidence: Dict[str, Any]) -> FusionCandidatesEnvelope:
    return {
        "candidates": candidates,
        "evidence": evidence,
    }


def _missing_visual_reason(faces_present: bool, faces_fresh: bool, candidates: List[Dict[str, Any]]) -> str:
    if candidates:
        return "audio_rescue"
    if faces_present and not faces_fresh:
        return "visual_stale_no_audio_rescue"
    if not faces_present:
        return "visual_missing_no_audio_rescue"
    return "no_candidates"
