"""
CONTRACT: inline (source: src/focusfield/ui/telemetry.md)
ROLE: Aggregate telemetry for UI.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.speaker_heatmap  Type: DoaHeatmap
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: uma8_leds.state  Type: Uma8LedState
  - Topic: log.events  Type: LogEvent
OUTPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot

CONFIG KEYS:
  - ui.telemetry_hz: update rate

PERF / TIMING:
  - stable update rate

FAILURE MODES:
  - missing inputs -> partial telemetry -> log telemetry_partial

LOG EVENTS:
  - module=ui.telemetry, event=telemetry_partial, payload keys=missing

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/telemetry.md):
# Telemetry contract

- Compact merged state for UI.
- Includes heatmap, lock state, and face summaries.
- Versioned for forward compatibility.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional

from focusfield.core.clock import now_ns


def start_telemetry(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    telemetry_hz = float(config.get("ui", {}).get("telemetry_hz", 10.0))
    q_heatmap = bus.subscribe("vision.speaker_heatmap")
    q_audio = bus.subscribe("audio.doa_heatmap")
    q_faces = bus.subscribe("vision.face_tracks")
    q_candidates = bus.subscribe("fusion.candidates")
    q_lock = bus.subscribe("fusion.target_lock")
    q_uma8_leds = bus.subscribe("uma8_leds.state")
    q_vision_debug = bus.subscribe("vision.face_tracks.debug")
    q_logs = bus.subscribe("log.events")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_health = bus.subscribe("runtime.health")
    q_perf = bus.subscribe("runtime.perf")
    q_vad = bus.subscribe("audio.vad")
    configured_cameras = [
        cam.get("id", f"cam{idx}")
        for idx, cam in enumerate(config.get("video", {}).get("cameras", []))
        if isinstance(cam, dict)
    ]
    configured_camera_map = [
        {
            "id": str(cam.get("id", f"cam{idx}")),
            "device_path": str(cam.get("device_path", "") or ""),
            "device_index": cam.get("device_index"),
            "yaw_offset_deg": float(cam.get("yaw_offset_deg", 0.0) or 0.0),
            "hfov_deg": float(cam.get("hfov_deg", 0.0) or 0.0),
        }
        for idx, cam in enumerate(config.get("video", {}).get("cameras", []))
        if isinstance(cam, dict)
    ]

    state: Dict[str, Any] = {
        "heatmap": None,
        "audio_heatmap": None,
        "faces": [],
        "candidates": [],
        "lock": None,
        "uma8_leds": None,
        "vision_debug": {},
        "logs": [],
        "beam": None,
        "health": None,
        "perf": None,
        "vad": None,
        "overflow_prev_total": None,
        "overflow_window": 0,
        "configured_cameras": configured_cameras,
        "configured_camera_map": configured_camera_map,
        "runtime_profile": str(config.get("runtime", {}).get("perf_profile", "default") or "default"),
        "strict_requirements_passed": bool(
            config.get("runtime", {}).get("requirements_passed", False)
            or not bool(config.get("runtime", {}).get("requirements", {}).get("strict", False))
        ),
        "detector_backend_active": str(config.get("runtime", {}).get("detector_backend_active", "unknown") or "unknown"),
    }
    seq = 0

    def _drain(q: queue.Queue) -> Optional[Any]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            return item
        return item

    def _run() -> None:
        nonlocal seq
        period = 1.0 / telemetry_hz if telemetry_hz > 0 else 0.1
        next_tick = time.time()
        while not stop_event.is_set():
            heatmap = _drain(q_heatmap)
            if heatmap is not None:
                state["heatmap"] = heatmap
            audio_heatmap = _drain(q_audio)
            if audio_heatmap is not None:
                state["audio_heatmap"] = audio_heatmap
            faces = _drain(q_faces)
            if faces is not None:
                state["faces"] = faces
            candidates = _drain(q_candidates)
            if candidates is not None:
                state["candidates"] = candidates
            lock_msg = _drain(q_lock)
            if lock_msg is not None:
                state["lock"] = lock_msg
            led_msg = _drain(q_uma8_leds)
            if led_msg is not None:
                state["uma8_leds"] = led_msg
            vision_debug_msg = _drain(q_vision_debug)
            if vision_debug_msg is not None:
                state["vision_debug"] = vision_debug_msg
            beam_msg = _drain(q_beam)
            if beam_msg is not None:
                state["beam"] = beam_msg
            health_msg = _drain(q_health)
            if health_msg is not None:
                state["health"] = health_msg
            perf_msg = _drain(q_perf)
            if perf_msg is not None:
                state["perf"] = perf_msg
                overflow_total: Optional[int] = None
                if isinstance(perf_msg, dict):
                    audio_capture = perf_msg.get("audio_capture")
                    if isinstance(audio_capture, dict):
                        raw_total = audio_capture.get(
                            "status_input_overflow_total",
                            audio_capture.get("status_input_overflow", 0),
                        )
                        try:
                            overflow_total = int(raw_total or 0)
                        except Exception:
                            overflow_total = None
                if overflow_total is not None:
                    prev_total = state.get("overflow_prev_total")
                    if prev_total is None:
                        state["overflow_window"] = 0
                    else:
                        try:
                            state["overflow_window"] = max(0, overflow_total - int(prev_total))
                        except Exception:
                            state["overflow_window"] = 0
                    state["overflow_prev_total"] = overflow_total
            vad_msg = _drain(q_vad)
            if vad_msg is not None:
                state["vad"] = vad_msg
            log_event = _drain(q_logs)
            if log_event is not None:
                logs: List[Dict[str, Any]] = state["logs"]
                logs.append(log_event)
                state["logs"] = logs[-50:]

            now = time.time()
            if now < next_tick:
                time.sleep(0.005)
                continue
            next_tick = now + period
            seq += 1
            snapshot = _build_snapshot(state, seq)
            bus.publish("ui.telemetry", snapshot)

    thread = threading.Thread(target=_run, name="ui-telemetry", daemon=True)
    thread.start()
    return thread


def _build_snapshot(state: Dict[str, Any], seq: int) -> Dict[str, Any]:
    audio_heatmap = state.get("audio_heatmap") or {}
    vision_heatmap = state.get("heatmap") or {}
    heatmap = audio_heatmap or {}
    lock_state = state.get("lock") or {}
    led_state = state.get("uma8_leds") or {}
    vision_debug_state = state.get("vision_debug") or {}
    if not isinstance(vision_debug_state, dict):
        vision_debug_state = {}
    faces = state.get("faces") or []
    candidates = state.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    vad_state = state.get("vad") or {}
    logs: List[Dict[str, Any]] = state.get("logs", [])
    no_candidates: Dict[str, Any] = {}
    for event in reversed(logs):
        if not isinstance(event, dict):
            continue
        ctx = event.get("context") or {}
        if not isinstance(ctx, dict):
            continue
        if ctx.get("module") == "fusion.av_association" and ctx.get("event") == "no_candidates":
            details = ctx.get("details") or {}
            if isinstance(details, dict):
                no_candidates = details
            break
    peaks = audio_heatmap.get("peaks", []) if isinstance(audio_heatmap, dict) else []
    top_peak_score = 0.0
    if isinstance(peaks, list) and peaks and isinstance(peaks[0], dict):
        top_peak_score = float(peaks[0].get("score", 0.0) or 0.0)
    doa_confidence = float((audio_heatmap or {}).get("confidence", 0.0) or 0.0)
    active_thresholds = lock_state.get("active_thresholds", {})
    if not isinstance(active_thresholds, dict):
        active_thresholds = {}
    perf_summary = state.get("perf") or {}
    if not isinstance(perf_summary, dict):
        perf_summary = {}
    else:
        perf_summary = dict(perf_summary)
    perf_fusion_debug = perf_summary.get("fusion_debug")
    if not isinstance(perf_fusion_debug, dict):
        perf_fusion_debug = {}
    perf_vision_debug = perf_summary.get("vision_debug")
    if not isinstance(perf_vision_debug, dict):
        perf_vision_debug = {}
    perf_audio_capture = perf_summary.get("audio_capture")
    if isinstance(perf_audio_capture, dict):
        perf_audio_capture = dict(perf_audio_capture)
        raw_total = perf_audio_capture.get(
            "status_input_overflow_total",
            perf_audio_capture.get("status_input_overflow", 0),
        )
        try:
            overflow_total = int(raw_total or 0)
        except Exception:
            overflow_total = 0
        perf_audio_capture["status_input_overflow_total"] = overflow_total
        perf_audio_capture["status_input_overflow_window"] = int(state.get("overflow_window", 0) or 0)
        perf_summary["audio_capture"] = perf_audio_capture
    active_face_cameras = sorted({face.get("camera_id") for face in faces if face.get("camera_id")})
    configured_cameras = [str(cam) for cam in (state.get("configured_cameras") or [])]
    cameras = configured_cameras if configured_cameras else active_face_cameras
    face_lock_scores: Dict[str, Dict[str, Any]] = {}
    ranked_candidates = sorted(
        [cand for cand in candidates if isinstance(cand, dict) and cand.get("track_id") is not None],
        key=lambda cand: float(cand.get("combined_score", cand.get("smoothed_score", 0.0)) or 0.0),
        reverse=True,
    )
    for idx, cand in enumerate(ranked_candidates, start=1):
        track_id = str(cand.get("track_id"))
        comps = cand.get("score_components", {})
        if not isinstance(comps, dict):
            comps = {}
        face_lock_scores[track_id] = {
            "track_id": track_id,
            "score": float(cand.get("combined_score", cand.get("smoothed_score", 0.0)) or 0.0),
            "rank": int(idx),
            "mode": str(cand.get("mode", "") or ""),
            "mouth": float(comps.get("mouth_activity", 0.0) or 0.0),
            "face": float(comps.get("face_confidence", 0.0) or 0.0),
            "doa": float(comps.get("doa_peak_score", 0.0) or 0.0),
            "angle_error_deg": float(cand.get("angular_distance_deg", 0.0) or 0.0),
            "facing_score": float(comps.get("facing_score", 0.0) or 0.0),
        }
    active_track_id = str(lock_state.get("target_id", "") or "")
    active_track_score = None
    if active_track_id and active_track_id in face_lock_scores:
        active_track_score = float(face_lock_scores[active_track_id].get("score", 0.0))
    detector_degraded = vision_debug_state.get("detector_degraded", {})
    if isinstance(detector_degraded, dict):
        detector_degraded_payload = {
            "active": bool(detector_degraded.get("active", False)),
            "reason": detector_degraded.get("reason"),
            "reasons_by_camera": detector_degraded.get("reasons_by_camera", {}),
        }
    else:
        detector_degraded_payload = {"active": bool(detector_degraded), "reason": "", "reasons_by_camera": {}}
    detector_backend_active = str(
        vision_debug_state.get("detector_backend")
        or state.get("detector_backend_active")
        or "unknown"
    )
    bus_drop_counts_window = perf_summary.get("bus_drop_counts_window", {})
    if not isinstance(bus_drop_counts_window, dict):
        bus_drop_counts_window = {}
    audio_fallback_active = str(lock_state.get("mode", "NO_LOCK") or "NO_LOCK") == "AUDIO_ONLY"
    beam_state = state.get("beam")
    if isinstance(beam_state, dict):
        beam_payload = dict(beam_state)
        beam_method = str(beam_payload.get("method", "") or "").strip().lower()
        beam_payload["runtime_state"] = beam_payload.get(
            "runtime_state",
            "fallback"
            if beam_payload.get("fallback_active")
            else ("mvdr_active" if beam_method == "mvdr" else "active"),
        )
    else:
        beam_payload = {"runtime_state": "disabled"}
    return {
        "t_ns": now_ns(),
        "seq": seq,
        "runtime_profile": state.get("runtime_profile", "default"),
        "strict_requirements_passed": bool(state.get("strict_requirements_passed", False)),
        "detector_backend_active": detector_backend_active,
        "audio_fallback_active": bool(audio_fallback_active),
        "bus_drop_counts_window": bus_drop_counts_window,
        "capture_overflow_window": int(state.get("overflow_window", 0) or 0),
        "heatmap_summary": {
            "bins": audio_heatmap.get("bins", 0),
            "bin_size_deg": audio_heatmap.get("bin_size_deg", 0.0),
            "peaks": audio_heatmap.get("peaks", []),
            "confidence": audio_heatmap.get("confidence", 0.0),
            "heatmap": audio_heatmap.get("heatmap", []),
        },
        "vision_heatmap_summary": {
            "bins": vision_heatmap.get("bins", 0),
            "bin_size_deg": vision_heatmap.get("bin_size_deg", 0.0),
            "peaks": vision_heatmap.get("peaks", []),
            "confidence": vision_heatmap.get("confidence", 0.0),
            "heatmap": vision_heatmap.get("heatmap", []),
        },
        "lock_state": {
            "state": lock_state.get("state", "NO_LOCK"),
            "mode": lock_state.get("mode", "NO_LOCK"),
            "target_bearing_deg": lock_state.get("target_bearing_deg"),
            "confidence": lock_state.get("confidence", 0.0),
            "reason": lock_state.get("reason", ""),
            "target_id": lock_state.get("target_id"),
        },
        "face_summaries": [
            {
                "track_id": face.get("track_id"),
                "bearing_deg": face.get("bearing_deg"),
                "mouth_activity": face.get("mouth_activity"),
                "mouth_motion_score": face.get("mouth_motion_score"),
                "mouth_aperture_score": face.get("mouth_aperture_score"),
                "face_angle_deg": face.get("face_angle_deg"),
                "facing_score": face.get("facing_score"),
                "speaking_evidence": face.get("speaking_evidence"),
                "speaking": face.get("speaking"),
                "bbox": face.get("bbox"),
                "camera_id": face.get("camera_id"),
                "confidence": face.get("confidence"),
            }
            for face in faces
        ],
        "beamformer": beam_payload,
        "uma8_leds": {
            "enabled": bool(led_state.get("enabled", False)),
            "backend": led_state.get("backend", "none"),
            "active_backend": led_state.get("backend", "none"),
            "preferred_backend": led_state.get("preferred_backend", led_state.get("backend", "none")),
            "state": led_state.get("state", "NO_LOCK"),
            "sector": led_state.get("sector"),
            "sectors": led_state.get("sectors", []),
            "brightness": led_state.get("brightness", 0.0),
            "rgb": led_state.get("rgb", [0, 0, 0]),
            "mapped_bearing_deg": led_state.get("mapped_bearing_deg"),
            "base_bearing_offset_deg": led_state.get("base_bearing_offset_deg", 0.0),
            "transport_error": led_state.get("transport_error", ""),
            "device_count": led_state.get("device_count"),
        },
        "fusion_debug": {
            "no_candidate_reason": no_candidates.get("reason", lock_state.get("reason", "")),
            "faces_present": bool(no_candidates.get("faces_present", bool(faces))),
            "faces_fresh": bool(no_candidates.get("faces_fresh", bool(faces))),
            "vad_speech": bool(vad_state.get("speech", False)),
            "doa_confidence": doa_confidence,
            "doa_peak_score": top_peak_score,
            "candidate_count": int(lock_state.get("candidate_count", 0) or 0),
            "last_candidate_mode": str(lock_state.get("best_candidate_mode", "") or ""),
            "active_acquire_threshold": active_thresholds.get("acquire"),
            "active_drop_threshold": active_thresholds.get("drop"),
            "face_lock_scores": face_lock_scores,
            "active_track_score": active_track_score,
            "handoff_margin": lock_state.get("handoff_margin"),
            "current_mode": str(lock_state.get("current_mode", lock_state.get("mode", "NO_LOCK")) or "NO_LOCK"),
            "interruptions_committed": int(perf_fusion_debug.get("interruptions_committed", 0) or 0),
            "lock_dwell_ms": float(perf_fusion_debug.get("lock_dwell_ms", 0.0) or 0.0),
            "fallback_dwell_ms": float(perf_fusion_debug.get("fallback_dwell_ms", 0.0) or 0.0),
        },
        "vision_debug": {
            "detector_backend": detector_backend_active,
            "detect_fps_by_camera": vision_debug_state.get("detect_fps_by_camera", {}),
            "face_count_by_camera": vision_debug_state.get("face_count_by_camera", {}),
            "last_detect_ms_by_camera": vision_debug_state.get("last_detect_ms_by_camera", {}),
            "roi_mode_by_camera": vision_debug_state.get("roi_mode_by_camera", {}),
            "detector_degraded": detector_degraded_payload,
            "face_reacquire_ms_p50": perf_vision_debug.get("face_reacquire_ms_p50"),
            "face_reacquire_ms_p95": perf_vision_debug.get("face_reacquire_ms_p95"),
        },
        "health_summary": state.get("health") or {},
        "perf_summary": perf_summary,
        "logs": logs,
        "meta": {
            "cameras": cameras,
            "active_face_cameras": active_face_cameras,
            "camera_map": state.get("configured_camera_map") or [],
        },
    }
