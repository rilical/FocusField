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
) -> Optional[threading.Thread]:
    ui_cfg = config.get("ui", {})
    if not isinstance(ui_cfg, dict):
        ui_cfg = {}
    if not bool(ui_cfg.get("enabled", True)):
        return None
    telemetry_hz = float(ui_cfg.get("telemetry_hz", 10.0))
    q_heatmap = bus.subscribe("vision.speaker_heatmap")
    q_audio = bus.subscribe("audio.doa_heatmap")
    q_faces = bus.subscribe("vision.face_tracks")
    q_faces_debug = bus.subscribe("vision.face_tracks.debug")
    q_candidates = bus.subscribe("fusion.candidates")
    q_lock = bus.subscribe("fusion.target_lock")
    q_uma8_leds = bus.subscribe("uma8_leds.state")
    q_logs = bus.subscribe("log.events")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_health = bus.subscribe("runtime.health")
    q_perf = bus.subscribe("runtime.perf")
    q_vad = bus.subscribe("audio.vad")
    q_mic_health = bus.subscribe("audio.mic_health")
    q_output = bus.subscribe("audio.output.stats")
    q_camera_calibration = bus.subscribe("vision.camera_calibration")
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
            "bearing_model": str(cam.get("bearing_model", "linear") or "linear").lower(),
            "bearing_offset_deg": float(cam.get("bearing_offset_deg", 0.0) or 0.0),
            "bearing_lut_path": str(cam.get("bearing_lut_path", "") or ""),
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
        "candidates": [],
        "candidates_evidence": {},
        "uma8_leds": None,
        "logs": [],
        "beam": None,
        "health": None,
        "perf": None,
        "vad": None,
        "mic_health": None,
        "output": None,
        "runtime_cfg": config.get("runtime", {}) if isinstance(config.get("runtime", {}), dict) else {},
        "runtime_profile": str(config.get("runtime", {}).get("perf_profile", "") or ""),
        "strict_requirements_passed": bool(config.get("runtime", {}).get("requirements_passed", False)),
        "audio_vad_enabled": bool(config.get("audio", {}).get("vad", {}).get("enabled", True)),
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
            cand_msg = _drain(q_candidates)
            if cand_msg is not None:
                if isinstance(cand_msg, dict) and "candidates" in cand_msg:
                    candidates = cand_msg.get("candidates", [])
                    state["candidates"] = candidates if isinstance(candidates, list) else []
                    evidence = cand_msg.get("evidence", {})
                    state["candidates_evidence"] = evidence if isinstance(evidence, dict) else {}
                else:
                    state["candidates"] = cand_msg if isinstance(cand_msg, list) else []
                    state["candidates_evidence"] = {}
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
            output_msg = _drain(q_output)
            if output_msg is not None:
                state["output"] = output_msg
            calibration_msg = _drain(q_camera_calibration)
            if isinstance(calibration_msg, dict):
                state["configured_camera_map"] = _merge_camera_map(
                    state.get("configured_camera_map") or [],
                    calibration_msg.get("cameras", []),
                )
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
    candidates = state.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    candidates_evidence = state.get("candidates_evidence") or {}
    if not isinstance(candidates_evidence, dict):
        candidates_evidence = {}
    output_state = state.get("output") or {}
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
    vad_confidence = float(vad_state.get("confidence", vad_state.get("speech_probability", 0.0)) or 0.0)
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
    beam_state = state.get("beam") or {}
    if not isinstance(beam_state, dict):
        beam_state = {}
    audio_fallback_active = bool(beam_state.get("fallback_active", False)) or str(lock_state.get("mode", "") or "") == "AUDIO_ONLY"
    vision_debug = state.get("vision_debug") or {}
    if not isinstance(vision_debug, dict):
        vision_debug = {}
    detector_degraded = vision_debug.get("detector_degraded", runtime_cfg.get("detector_backend_degraded", False))
    if isinstance(detector_degraded, dict):
        detector_degraded = bool(detector_degraded.get("active", False))
    active_face_cameras = sorted({face.get("camera_id") for face in faces if face.get("camera_id")})
    configured_cameras = [str(cam) for cam in (state.get("configured_cameras") or [])]
    cameras = configured_cameras if configured_cameras else active_face_cameras
    mic_health_summary = _summarize_mic_health(state.get("mic_health") or {})
    top_candidates = _summarize_candidates(candidates)
    top_focus_score = float(top_candidates[0].get("focus_score", 0.0)) if top_candidates else 0.0
    runner_up_focus_score = float(top_candidates[1].get("focus_score", 0.0)) if len(top_candidates) > 1 else 0.0
    audio_route_summary = _summarize_audio_route(runtime_cfg, output_state if isinstance(output_state, dict) else {})
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
            "focus_score": lock_state.get("focus_score", lock_state.get("confidence", 0.0)),
            "activity_score": lock_state.get("activity_score", 0.0),
            "selection_mode": lock_state.get("selection_mode", lock_state.get("mode", "NO_LOCK")),
            "score_margin": lock_state.get("score_margin", 0.0),
            "runner_up_focus_score": lock_state.get("runner_up_focus_score", runner_up_focus_score),
            "reason": lock_state.get("reason", ""),
            "target_id": lock_state.get("target_id"),
            "target_camera_id": lock_state.get("target_camera_id"),
            "active_thresholds": lock_state.get("active_thresholds", {}),
            "timing_window_ms": lock_state.get("timing_window_ms", {}),
            "evidence_status": lock_state.get("evidence_status", {}),
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
                "visual_quality": face.get("visual_quality"),
                "motion_activity": face.get("motion_activity"),
                "landmark_presence": face.get("landmark_presence"),
                "visual_backend": face.get("visual_backend"),
            }
            for face in faces
        ],
        "top_candidates": top_candidates,
        "beamformer": state.get("beam"),
        "mic_health": state.get("mic_health") or {},
        "mic_health_summary": mic_health_summary,
        "output_summary": output_state if isinstance(output_state, dict) else {},
        "audio_route_summary": audio_route_summary,
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
            "no_candidate_reason": candidates_evidence.get("reason", no_candidates.get("reason", lock_state.get("reason", ""))),
            "faces_present": bool(candidates_evidence.get("faces_present", no_candidates.get("faces_present", bool(faces)))),
            "faces_fresh": bool(candidates_evidence.get("faces_fresh", no_candidates.get("faces_fresh", bool(faces)))),
            "vad_speech": bool(vad_state.get("speech", False)),
            "vad_confidence": vad_confidence,
            "doa_confidence": doa_confidence,
            "doa_peak_score": top_peak_score,
            "focus_score": lock_state.get("focus_score", top_focus_score),
            "score_margin": lock_state.get("score_margin", max(0.0, top_focus_score - runner_up_focus_score)),
            "runner_up_focus_score": lock_state.get("runner_up_focus_score", runner_up_focus_score),
            "audio_fresh": bool(candidates_evidence.get("audio_fresh", False)),
            "audio_stale": bool(candidates_evidence.get("audio_stale", False)),
            "visual_stale": bool(candidates_evidence.get("visual_stale", False)),
            "disagreement_suppressed": bool(
                candidates_evidence.get("disagreement_suppressed", (lock_state.get("evidence_status") or {}).get("disagreement_suppressed", False))
            ),
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
            "audio_vad_enabled": bool(state.get("audio_vad_enabled", True)),
            "camera_map": state.get("configured_camera_map") or [],
            "audio_device": runtime_cfg.get("selected_audio_device", {}),
            "camera_calibration_overlay": runtime_cfg.get("camera_calibration_overlay", {}),
            "audio_calibration_overlay": runtime_cfg.get("audio_calibration_overlay", {}),
            "runtime_config": {
                "config_path": runtime_cfg.get("config_path", ""),
                "config_effective_path": runtime_cfg.get("config_effective_path", ""),
                "config_basename": runtime_cfg.get("config_basename", ""),
                "generated_from_base_config": runtime_cfg.get("generated_from_base_config", ""),
                "generated_from_base_basename": runtime_cfg.get("generated_from_base_basename", ""),
                "generated_for_pi": bool(runtime_cfg.get("generated_for_pi", False)),
                "process_mode": runtime_cfg.get("process_mode", ""),
                "perf_profile": runtime_profile,
                "thresholds_preset": runtime_cfg.get("thresholds_preset_active", ""),
                "requirements": runtime_cfg.get("requirements", {}),
                "audio_device_profile": runtime_cfg.get("audio_device_profile", ""),
                "audio_yaw_offset_deg": float(runtime_cfg.get("audio_yaw_offset_deg", 0.0) or 0.0),
                "audio_yaw_calibration": runtime_cfg.get("audio_yaw_calibration", {}),
            },
        },
}


def _summarize_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summarized: List[Dict[str, Any]] = []
    for cand in sorted(candidates, key=lambda item: float(item.get("focus_score", item.get("combined_score", 0.0)) or 0.0), reverse=True)[:3]:
        score_components = cand.get("score_components", {})
        if not isinstance(score_components, dict):
            score_components = {}
        summarized.append(
            {
                "track_id": cand.get("track_id"),
                "camera_id": cand.get("camera_id"),
                "bearing_deg": cand.get("bearing_deg"),
                "focus_score": float(cand.get("focus_score", cand.get("combined_score", 0.0)) or 0.0),
                "activity_score": float(cand.get("activity_score", cand.get("speaking_probability", 0.0)) or 0.0),
                "selection_mode": str(cand.get("selection_mode", "")),
                "speaking": bool(cand.get("speaking", False)),
                "score_components": {
                    "visual_speaking_prob": float(score_components.get("visual_speaking_prob", score_components.get("mouth_activity", 0.0)) or 0.0),
                    "doa_peak_score": float(score_components.get("doa_peak_score", 0.0) or 0.0),
                    "doa_confidence": float(score_components.get("doa_confidence", 0.0) or 0.0),
                    "audio_speech_prob": float(score_components.get("audio_speech_prob", 0.0) or 0.0),
                },
                "score_groups": cand.get("score_groups", {}),
                "evidence_status": cand.get("evidence_status", {}),
            }
        )
    return summarized


def _merge_camera_map(current: List[Dict[str, Any]], updates: Any) -> List[Dict[str, Any]]:
    if not isinstance(current, list):
        current = []
    merged: List[Dict[str, Any]] = [dict(item) for item in current if isinstance(item, dict)]
    by_id = {str(item.get("id", "")): item for item in merged}
    if not isinstance(updates, list):
        return merged
    for item in updates:
        if not isinstance(item, dict):
            continue
        camera_id = str(item.get("id", "") or "").strip()
        if not camera_id:
            continue
        target = by_id.get(camera_id)
        if target is None:
            target = {"id": camera_id}
            merged.append(target)
            by_id[camera_id] = target
        if "yaw_offset_deg" in item:
            target["yaw_offset_deg"] = float(item.get("yaw_offset_deg", target.get("yaw_offset_deg", 0.0)) or 0.0)
        if "bearing_model" in item:
            target["bearing_model"] = str(item.get("bearing_model", target.get("bearing_model", "linear")) or "linear").lower()
        if "bearing_offset_deg" in item:
            target["bearing_offset_deg"] = float(item.get("bearing_offset_deg", target.get("bearing_offset_deg", 0.0)) or 0.0)
        if "bearing_lut_path" in item:
            target["bearing_lut_path"] = str(item.get("bearing_lut_path", target.get("bearing_lut_path", "")) or "")
    return merged


def _summarize_mic_health(mic_health: Dict[str, Any]) -> Dict[str, Any]:
    entries = mic_health.get("channels", []) if isinstance(mic_health, dict) else []
    if not isinstance(entries, list):
        entries = []
    dead_channels: List[int] = []
    degraded_channels: List[int] = []
    bad_channels: List[int] = []
    weakest: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        channel_raw = entry.get("channel", -1)
        channel = int(channel_raw if channel_raw is not None else -1)
        if channel < 0:
            continue
        score = float(entry.get("score", 1.0) or 1.0)
        bad_reason = str(entry.get("bad_reason", "") or "")
        if bad_reason:
            bad_channels.append(channel)
        if "dead" in bad_reason or "dropout" in bad_reason:
            dead_channels.append(channel)
        elif bad_reason or score < 0.35:
            degraded_channels.append(channel)
        weakest.append({"channel": channel, "score": score, "bad_reason": bad_reason})
    weakest = sorted(weakest, key=lambda item: item["score"])[:3]
    return {
        "dead_channels": dead_channels,
        "degraded_channels": degraded_channels,
        "bad_channels": bad_channels,
        "active_channels": list(mic_health.get("active_channels", []) or []) if isinstance(mic_health, dict) else [],
        "mean_score": float(mic_health.get("mean_score", 0.0) or 0.0) if isinstance(mic_health, dict) else 0.0,
        "mean_trust": float(mic_health.get("mean_trust", 0.0) or 0.0) if isinstance(mic_health, dict) else 0.0,
        "weakest_channels": weakest,
    }


def _summarize_audio_route(runtime_cfg: Dict[str, Any], output_state: Dict[str, Any]) -> Dict[str, Any]:
    input_device = runtime_cfg.get("selected_audio_device", {})
    if not isinstance(input_device, dict):
        input_device = {}
    input_name = str(input_device.get("device_name", "") or "")
    output_name = str(output_state.get("device_name", "") or "")
    output_sink = str(output_state.get("sink", "") or "")
    same_device = bool(input_name and output_name and input_name.strip().lower() == output_name.strip().lower())
    input_loopback = _looks_like_loopback(input_name)
    output_blackhole = "blackhole" in output_name.strip().lower()
    return {
        "input_device_index": input_device.get("device_index"),
        "input_device_name": input_name,
        "input_channels": int(input_device.get("channels", 0) or 0),
        "output_sink": output_sink,
        "output_device_name": output_name,
        "output_blackhole_active": bool(output_blackhole),
        "input_loopback_risk": bool(input_loopback or same_device),
        "same_input_output_device": same_device,
        "output_underrun_window": int(output_state.get("underrun_window", 0) or 0),
        "output_underrun_total": int(output_state.get("underrun_total", 0) or 0),
        "output_device_error_total": int(output_state.get("device_error_total", 0) or 0),
        "output_occupancy_frames": int(output_state.get("occupancy_frames", 0) or 0),
        "output_buffer_capacity_frames": int(output_state.get("buffer_capacity_frames", 0) or 0),
        "output_input_age_ms": output_state.get("input_age_ms"),
    }


def _looks_like_loopback(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    return any(token in lowered for token in ("blackhole", "loopback", "soundflower", "vb-cable", "zoom audio", "teams audio"))
