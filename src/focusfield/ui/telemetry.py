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
    q_faces_debug = bus.subscribe("vision.face_tracks.debug")
    q_lock = bus.subscribe("fusion.target_lock")
    q_uma8_leds = bus.subscribe("uma8_leds.state")
    q_logs = bus.subscribe("log.events")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_health = bus.subscribe("runtime.health")
    q_perf = bus.subscribe("runtime.perf")
    q_vad = bus.subscribe("audio.vad")
    q_mic_health = bus.subscribe("audio.mic_health")
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
        "lock": None,
        "uma8_leds": None,
        "logs": [],
        "beam": None,
        "health": None,
        "perf": None,
        "vad": None,
        "mic_health": None,
        "runtime_cfg": config.get("runtime", {}) if isinstance(config.get("runtime", {}), dict) else {},
        "runtime_profile": str(config.get("runtime", {}).get("perf_profile", "") or ""),
        "strict_requirements_passed": bool(config.get("runtime", {}).get("requirements_passed", False)),
        "detector_backend_active": str(config.get("runtime", {}).get("detector_backend_active", "") or ""),
        "vision_debug": {
            "detector_backend": str(config.get("runtime", {}).get("detector_backend_active", "") or ""),
            "detector_degraded": bool(config.get("runtime", {}).get("detector_backend_degraded", False)),
            "detector_reason": str(config.get("runtime", {}).get("detector_backend_reason", "") or ""),
        },
        "overflow_window": 0,
        "configured_cameras": configured_cameras,
        "configured_camera_map": configured_camera_map,
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
            faces_debug = _drain(q_faces_debug)
            if faces_debug is not None:
                state["vision_debug"] = {
                    **(state.get("vision_debug") or {}),
                    **(faces_debug if isinstance(faces_debug, dict) else {}),
                }
            lock_msg = _drain(q_lock)
            if lock_msg is not None:
                state["lock"] = lock_msg
            led_msg = _drain(q_uma8_leds)
            if led_msg is not None:
                state["uma8_leds"] = led_msg
            beam_msg = _drain(q_beam)
            if beam_msg is not None:
                state["beam"] = beam_msg
            health_msg = _drain(q_health)
            if health_msg is not None:
                state["health"] = health_msg
            perf_msg = _drain(q_perf)
            if perf_msg is not None:
                state["perf"] = perf_msg
                summary = perf_msg.get("summary") if isinstance(perf_msg, dict) else {}
                if not isinstance(summary, dict):
                    summary = {}
                capture_summary = summary.get("audio_capture") if isinstance(summary.get("audio_capture"), dict) else {}
                state["overflow_window"] = int(
                    capture_summary.get("status_input_overflow_window", perf_msg.get("status_input_overflow_window", 0)) or 0
                )
            vad_msg = _drain(q_vad)
            if vad_msg is not None:
                state["vad"] = vad_msg
            mic_health_msg = _drain(q_mic_health)
            if mic_health_msg is not None:
                state["mic_health"] = mic_health_msg
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
    heatmap = state.get("audio_heatmap") or {}
    vision_heatmap = state.get("heatmap") or {}
    lock_state = state.get("lock") or {}
    led_state = state.get("uma8_leds") or {}
    faces = state.get("faces") or []
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
    peaks = heatmap.get("peaks", [])
    top_peak_score = 0.0
    if isinstance(peaks, list) and peaks and isinstance(peaks[0], dict):
        top_peak_score = float(peaks[0].get("score", 0.0) or 0.0)
    doa_confidence = float(heatmap.get("confidence", 0.0) or 0.0)
    perf_state = state.get("perf") or {}
    perf_summary = perf_state.get("summary") if isinstance(perf_state, dict) else {}
    if not isinstance(perf_summary, dict):
        perf_summary = perf_state if isinstance(perf_state, dict) else {}
    bus_drop_counts_window = (
        perf_summary.get("bus_drop_counts_window")
        if isinstance(perf_summary.get("bus_drop_counts_window"), dict)
        else perf_state.get("bus_drop_counts_window", {})
    )
    if not isinstance(bus_drop_counts_window, dict):
        bus_drop_counts_window = {}
    runtime_cfg = state.get("runtime_cfg") if isinstance(state.get("runtime_cfg"), dict) else {}
    runtime_profile = state.get("runtime_profile") or runtime_cfg.get("perf_profile") or ""
    strict_requirements_passed = bool(
        state.get("strict_requirements_passed", runtime_cfg.get("requirements_passed", False))
    )
    detector_backend_active = state.get("detector_backend_active") or runtime_cfg.get("detector_backend_active") or ""
    audio_fallback_active = str(lock_state.get("mode", "") or "") == "AUDIO_ONLY"
    vision_debug = state.get("vision_debug") or {}
    if not isinstance(vision_debug, dict):
        vision_debug = {}
    detector_degraded = vision_debug.get("detector_degraded", runtime_cfg.get("detector_backend_degraded", False))
    if isinstance(detector_degraded, dict):
        detector_degraded = bool(detector_degraded.get("active", False))
    active_face_cameras = sorted({face.get("camera_id") for face in faces if face.get("camera_id")})
    configured_cameras = [str(cam) for cam in (state.get("configured_cameras") or [])]
    cameras = configured_cameras if configured_cameras else active_face_cameras
    return {
        "t_ns": now_ns(),
        "seq": seq,
        "heatmap_summary": {
            "bins": heatmap.get("bins", 0),
            "bin_size_deg": heatmap.get("bin_size_deg", 0.0),
            "peaks": heatmap.get("peaks", []),
            "confidence": heatmap.get("confidence", 0.0),
            "heatmap": heatmap.get("heatmap", []),
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
                "visual_speaking_prob": face.get("visual_speaking_prob", face.get("mouth_activity")),
                "speaking": face.get("speaking"),
                "bbox": face.get("bbox"),
                "camera_id": face.get("camera_id"),
                "confidence": face.get("confidence"),
            }
            for face in faces
        ],
        "beamformer": state.get("beam"),
        "mic_health": state.get("mic_health") or {},
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
        },
        "health_summary": state.get("health") or {},
        "perf_summary": perf_state,
        "runtime_profile": runtime_profile,
        "strict_requirements_passed": strict_requirements_passed,
        "detector_backend_active": detector_backend_active,
        "audio_fallback_active": audio_fallback_active,
        "bus_drop_counts_window": bus_drop_counts_window,
        "capture_overflow_window": int(state.get("overflow_window", 0) or 0),
        "vision_debug": {
            **vision_debug,
            "detector_backend": detector_backend_active,
            "detector_degraded": bool(detector_degraded),
            "detector_reason": vision_debug.get("detector_reason", runtime_cfg.get("detector_backend_reason", "")),
        },
        "logs": logs,
        "meta": {
            "cameras": cameras,
            "active_face_cameras": active_face_cameras,
            "camera_map": state.get("configured_camera_map") or [],
        },
    }
