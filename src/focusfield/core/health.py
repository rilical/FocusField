"""focusfield.core.health

CONTRACT: inline (source: src/focusfield/core/health.md)
ROLE: Heartbeat aggregation and health summary.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: audio.enhanced.final  Type: EnhancedAudio
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: vision.frames.cam*  Type: VideoFrame

OUTPUTS:
  - Topic: runtime.health  Type: dict

CONFIG KEYS:
  - health.enabled: enable health monitor
  - health.thresholds_ms.audio_frames
  - health.thresholds_ms.enhanced_final
  - health.thresholds_ms.face_tracks
  - health.thresholds_ms.camera_frame

PERF / TIMING:
  - emits health snapshot at ~2 Hz
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from focusfield.core.clock import now_ns


@dataclass
class _TopicState:
    last_t_ns: int = 0
    last_wall_s: float = 0.0
    count: int = 0
    rate_hz: float = 0.0
    _last_rate_wall_s: float = 0.0
    _last_rate_count: int = 0

    def on_msg(self, msg: Dict[str, Any]) -> None:
        self.last_t_ns = int(msg.get("t_ns", self.last_t_ns or now_ns()))
        self.last_wall_s = time.time()
        self.count += 1
        if not self._last_rate_wall_s:
            self._last_rate_wall_s = self.last_wall_s
            self._last_rate_count = self.count
            return
        dt = self.last_wall_s - self._last_rate_wall_s
        if dt >= 1.0:
            self.rate_hz = (self.count - self._last_rate_count) / max(dt, 1e-6)
            self._last_rate_wall_s = self.last_wall_s
            self._last_rate_count = self.count


def start_health_monitor(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    health_cfg = config.get("health", {})
    if not isinstance(health_cfg, dict):
        health_cfg = {}
    if not bool(health_cfg.get("enabled", True)):
        return None
    thresholds = health_cfg.get("thresholds_ms", {})
    if not isinstance(thresholds, dict):
        thresholds = {}
    th_audio = float(thresholds.get("audio_frames", 200))
    th_final = float(thresholds.get("enhanced_final", 300))
    th_faces = float(thresholds.get("face_tracks", 1000))
    th_cam = float(thresholds.get("camera_frame", 1000))

    cameras = [cam.get("id", f"cam{idx}") for idx, cam in enumerate(config.get("video", {}).get("cameras", []))]
    q_audio = bus.subscribe("audio.frames")
    q_final = bus.subscribe("audio.enhanced.final")
    q_faces = bus.subscribe("vision.face_tracks")
    q_lock = bus.subscribe("fusion.target_lock")
    q_perf = bus.subscribe("runtime.perf")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_mic_health = bus.subscribe("audio.mic_health")
    q_cams = {cam_id: bus.subscribe(f"vision.frames.{cam_id}") for cam_id in cameras}

    state = {
        "audio.frames": _TopicState(),
        "audio.enhanced.final": _TopicState(),
        "vision.face_tracks": _TopicState(),
        "fusion.target_lock": _TopicState(),
    }
    for cam_id in cameras:
        state[f"vision.frames.{cam_id}"] = _TopicState()
    seq = 0
    latest_perf: Dict[str, Any] = {}
    latest_beam: Dict[str, Any] = {}
    latest_mic_health: Dict[str, Any] = {}

    def _drain(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _run() -> None:
        nonlocal seq, latest_perf, latest_beam, latest_mic_health
        next_emit = time.time()
        while not stop_event.is_set():
            audio = _drain(q_audio)
            if audio is not None:
                state["audio.frames"].on_msg(audio)
            final = _drain(q_final)
            if final is not None:
                state["audio.enhanced.final"].on_msg(final)
            faces = _drain(q_faces)
            if faces is not None:
                state["vision.face_tracks"].on_msg({"t_ns": now_ns()})
            lock = _drain(q_lock)
            if lock is not None:
                state["fusion.target_lock"].on_msg(lock)
            perf = _drain(q_perf)
            if isinstance(perf, dict):
                latest_perf = perf
            beam = _drain(q_beam)
            if isinstance(beam, dict):
                latest_beam = beam
            mic_health = _drain(q_mic_health)
            if isinstance(mic_health, dict):
                latest_mic_health = mic_health
            for cam_id, q in q_cams.items():
                cam_msg = _drain(q)
                if cam_msg is not None:
                    state[f"vision.frames.{cam_id}"].on_msg(cam_msg)

            now = time.time()
            if now < next_emit:
                time.sleep(0.02)
                continue
            next_emit = now + 0.5
            seq += 1
            drop_counts = {}
            try:
                drop_counts = bus.get_drop_counts()
            except Exception:
                drop_counts = {}

            snapshot = _build_snapshot(
                state,
                seq,
                th_audio,
                th_final,
                th_faces,
                th_cam,
                latest_perf=latest_perf,
                latest_beam=latest_beam,
                latest_mic_health=latest_mic_health,
            )
            snapshot["bus"] = {"drop_counts": drop_counts}
            bus.publish("runtime.health", snapshot)
            if snapshot.get("status") == "degraded":
                logger.emit("debug", "core.health", "module_unhealthy", {"reasons": snapshot.get("reasons", [])})

    thread = threading.Thread(target=_run, name="health", daemon=True)
    thread.start()
    return thread


def _build_snapshot(
    state: Dict[str, _TopicState],
    seq: int,
    th_audio_ms: float,
    th_final_ms: float,
    th_faces_ms: float,
    th_cam_ms: float,
    latest_perf: Optional[Dict[str, Any]] = None,
    latest_beam: Optional[Dict[str, Any]] = None,
    latest_mic_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now_s = time.time()

    def age_ms(topic: str) -> Optional[float]:
        st = state.get(topic)
        if st is None or not st.last_wall_s:
            return None
        return (now_s - st.last_wall_s) * 1000.0

    reasons: List[Dict[str, Any]] = []
    a_audio = age_ms("audio.frames")
    if a_audio is None or a_audio > th_audio_ms:
        reasons.append({"topic": "audio.frames", "age_ms": a_audio, "threshold_ms": th_audio_ms})
    a_final = age_ms("audio.enhanced.final")
    if a_final is None or a_final > th_final_ms:
        reasons.append({"topic": "audio.enhanced.final", "age_ms": a_final, "threshold_ms": th_final_ms})
    a_faces = age_ms("vision.face_tracks")
    if a_faces is None or a_faces > th_faces_ms:
        reasons.append({"topic": "vision.face_tracks", "age_ms": a_faces, "threshold_ms": th_faces_ms})
    for topic in [k for k in state.keys() if k.startswith("vision.frames.")]:
        a_cam = age_ms(topic)
        if a_cam is None or a_cam > th_cam_ms:
            reasons.append({"topic": topic, "age_ms": a_cam, "threshold_ms": th_cam_ms})

    perf = latest_perf if isinstance(latest_perf, dict) else {}
    queue_pressure = perf.get("queue_pressure", {}) if isinstance(perf.get("queue_pressure", {}), dict) else {}
    drop_total_window = int(queue_pressure.get("drop_total_window", 0) or 0)
    capture_overflow_window = int(queue_pressure.get("capture_overflow_window", 0) or 0)
    if drop_total_window > 0:
        reasons.append({"topic": "runtime.perf", "reason": "queue_pressure", "drop_total_window": drop_total_window})
    if capture_overflow_window > 0:
        reasons.append(
            {
                "topic": "audio.capture",
                "reason": "input_overflow",
                "capture_overflow_window": capture_overflow_window,
            }
        )

    beam = latest_beam if isinstance(latest_beam, dict) else {}
    if bool(beam.get("fallback_active", False)):
        reasons.append({"topic": "audio.beamformer", "reason": "fallback_active"})

    mic_health = latest_mic_health if isinstance(latest_mic_health, dict) else {}
    bad_channels: List[int] = []
    entries = mic_health.get("channels")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            bad_reason = str(entry.get("bad_reason", "") or "")
            if "dead" in bad_reason or "dropout" in bad_reason:
                bad_channels.append(int(entry.get("channel", -1) or -1))
    if bad_channels:
        reasons.append(
            {
                "topic": "audio.mic_health",
                "reason": "dead_or_dropout_channels",
                "channels": [ch for ch in bad_channels if ch >= 0],
            }
        )

    status = "ok" if not reasons else "degraded"
    topics = {
        name: {
            "age_ms": age_ms(name),
            "rate_hz": float(st.rate_hz),
            "count": int(st.count),
        }
        for name, st in state.items()
    }
    return {
        "t_ns": now_ns(),
        "seq": seq,
        "status": status,
        "reasons": reasons,
        "topics": topics,
    }
