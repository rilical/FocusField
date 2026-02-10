"""
CONTRACT: inline (source: src/focusfield/ui/telemetry.md)
ROLE: Aggregate telemetry for UI.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.speaker_heatmap  Type: DoaHeatmap
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: fusion.target_lock  Type: TargetLock
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
    q_lock = bus.subscribe("fusion.target_lock")
    q_logs = bus.subscribe("log.events")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_health = bus.subscribe("runtime.health")
    q_perf = bus.subscribe("runtime.perf")

    state: Dict[str, Any] = {
        "heatmap": None,
        "audio_heatmap": None,
        "faces": [],
        "lock": None,
        "logs": [],
        "beam": None,
        "health": None,
        "perf": None,
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
            lock_msg = _drain(q_lock)
            if lock_msg is not None:
                state["lock"] = lock_msg
            beam_msg = _drain(q_beam)
            if beam_msg is not None:
                state["beam"] = beam_msg
            health_msg = _drain(q_health)
            if health_msg is not None:
                state["health"] = health_msg
            perf_msg = _drain(q_perf)
            if perf_msg is not None:
                state["perf"] = perf_msg
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
    heatmap = state.get("heatmap") or state.get("audio_heatmap") or {}
    lock_state = state.get("lock") or {}
    faces = state.get("faces") or []
    cameras = sorted({face.get("camera_id") for face in faces if face.get("camera_id")})
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
                "speaking": face.get("speaking"),
                "bbox": face.get("bbox"),
                "camera_id": face.get("camera_id"),
                "confidence": face.get("confidence"),
            }
            for face in faces
        ],
        "beamformer": state.get("beam"),
        "health_summary": state.get("health") or {},
        "perf_summary": state.get("perf") or {},
        "logs": state.get("logs", []),
        "meta": {
            "cameras": cameras,
        },
    }
