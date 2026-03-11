"""focusfield.core.perf_monitor

CONTRACT: docs/11_contract_index.md
ROLE: Emit lightweight runtime performance stats and persist them.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: audio.enhanced.final  Type: EnhancedAudio

OUTPUTS:
  - Topic: runtime.perf  Type: dict
  - artifacts/<run_id>/logs/perf.jsonl

CONFIG KEYS:
  - runtime.artifacts.dir_run: run directory path
  - perf.enabled: enable perf monitor
  - perf.emit_hz: publish rate
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from focusfield.core.clock import now_ns


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    p = min(100.0, max(0.0, float(pct)))
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(len(ordered) - 1, lo + 1)
    if lo == hi:
        return float(ordered[lo])
    alpha = rank - lo
    return float(ordered[lo] * (1.0 - alpha) + ordered[hi] * alpha)


def start_perf_monitor(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    perf_cfg = config.get("perf", {})
    if not isinstance(perf_cfg, dict):
        perf_cfg = {}
    if not bool(perf_cfg.get("enabled", True)):
        return None
    emit_hz = float(perf_cfg.get("emit_hz", 1.0))
    emit_hz = max(0.2, min(20.0, emit_hz))

    run_dir = config.get("runtime", {}).get("artifacts", {}).get("dir_run")
    path: Optional[Path] = None
    if run_dir:
        logs_dir = Path(str(run_dir)) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "perf.jsonl"

    q_audio = bus.subscribe("audio.frames")
    q_final = bus.subscribe("audio.enhanced.final")
    q_capture_stats = bus.subscribe("audio.capture.stats")
    q_worker = bus.subscribe("runtime.worker_loop")
    q_lock = bus.subscribe("fusion.target_lock")
    q_faces = bus.subscribe("vision.face_tracks")

    started_t_ns = now_ns()
    stats: Dict[str, Any] = {
        "audio_frames": {"last_t_ns": 0, "count": 0},
        "enhanced_final": {"last_t_ns": 0, "count": 0, "pipeline_queue_age_ms": None},
        "audio_capture": {
            "queue_depth": 0,
            "frames_enqueued": 0,
            "frames_published": 0,
            "callback_overflow_drop": 0,
            "status_input_overflow": 0,
            "status_input_overflow_total": 0,
            "status_input_overflow_window": 0,
        },
        "worker_loops": {},
        "fusion_debug": {
            "interruptions_committed": 0,
            "lock_dwell_ms": 0.0,
            "fallback_dwell_ms": 0.0,
        },
        "vision_debug": {
            "face_reacquire_ms_p50": None,
            "face_reacquire_ms_p95": None,
        },
    }
    overflow_prev_total: Optional[int] = None
    lock_state = "NO_LOCK"
    lock_state_since_ns = started_t_ns
    lock_dwell_ns = 0
    fallback_dwell_ns = 0
    interruption_count = 0
    last_lock_target_id: Optional[str] = None
    faces_present = False
    faces_absent_since_ns = started_t_ns
    face_reacquire_samples_ms: List[float] = []

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _drain_all(q: queue.Queue) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            while True:
                item = q.get_nowait()
                if isinstance(item, dict):
                    items.append(item)
        except queue.Empty:
            pass
        return items

    def _run() -> None:
        nonlocal overflow_prev_total
        nonlocal lock_state
        nonlocal lock_state_since_ns
        nonlocal lock_dwell_ns
        nonlocal fallback_dwell_ns
        nonlocal interruption_count
        nonlocal last_lock_target_id
        nonlocal faces_present
        nonlocal faces_absent_since_ns
        nonlocal face_reacquire_samples_ms
        fh = None
        try:
            if path is not None:
                fh = open(path, "a", encoding="utf-8")

            period = 1.0 / emit_hz if emit_hz > 0 else 1.0
            next_emit = time.time() + period
            prev_drop_counts: Dict[str, int] = {}
            prev_publish_counts: Dict[str, int] = {}
            while not stop_event.is_set():
                a = _drain_latest(q_audio)
                if a is not None:
                    stats["audio_frames"]["last_t_ns"] = int(a.get("t_ns", 0))
                    stats["audio_frames"]["count"] = int(stats["audio_frames"]["count"]) + 1
                f = _drain_latest(q_final)
                if f is not None:
                    t_ns = int(f.get("t_ns", 0))
                    stats["enhanced_final"]["last_t_ns"] = t_ns
                    stats["enhanced_final"]["count"] = int(stats["enhanced_final"]["count"]) + 1
                    queue_age_ms = (now_ns() - t_ns) / 1_000_000.0 if t_ns else None
                    stats["enhanced_final"]["pipeline_queue_age_ms"] = (
                        float(queue_age_ms) if queue_age_ms is not None else None
                    )
                cap_stats = _drain_latest(q_capture_stats)
                if isinstance(cap_stats, dict):
                    stats["audio_capture"]["queue_depth"] = int(cap_stats.get("queue_depth", 0) or 0)
                    stats["audio_capture"]["frames_enqueued"] = int(cap_stats.get("frames_enqueued", 0) or 0)
                    stats["audio_capture"]["frames_published"] = int(cap_stats.get("frames_published", 0) or 0)
                    stats["audio_capture"]["callback_overflow_drop"] = int(
                        cap_stats.get("callback_overflow_drop", 0) or 0
                    )
                    stats["audio_capture"]["status_input_overflow"] = int(
                        cap_stats.get("status_input_overflow", 0) or 0
                    )
                    overflow_total = int(
                        cap_stats.get(
                            "status_input_overflow_total",
                            cap_stats.get("status_input_overflow", 0),
                        )
                        or 0
                    )
                    stats["audio_capture"]["status_input_overflow_total"] = overflow_total
                    if overflow_prev_total is None:
                        stats["audio_capture"]["status_input_overflow_window"] = 0
                    else:
                        stats["audio_capture"]["status_input_overflow_window"] = max(0, overflow_total - overflow_prev_total)
                    overflow_prev_total = overflow_total
                lock_msg = _drain_latest(q_lock)
                if isinstance(lock_msg, dict):
                    msg_t_ns = int(lock_msg.get("t_ns", now_ns()) or now_ns())
                    state = str(lock_msg.get("state", "NO_LOCK") or "NO_LOCK")
                    if state != lock_state:
                        elapsed_ns = max(0, msg_t_ns - lock_state_since_ns)
                        if lock_state == "NO_LOCK":
                            fallback_dwell_ns += elapsed_ns
                        else:
                            lock_dwell_ns += elapsed_ns
                        lock_state = state
                        lock_state_since_ns = msg_t_ns
                    reason = str(lock_msg.get("reason", "") or "")
                    target_id = lock_msg.get("target_id")
                    if reason == "handoff_commit":
                        interruption_count += 1
                    elif target_id is not None and last_lock_target_id is not None and str(target_id) != str(last_lock_target_id):
                        interruption_count += 1
                    if target_id is not None:
                        last_lock_target_id = str(target_id)
                faces_msg = _drain_latest(q_faces)
                if isinstance(faces_msg, list):
                    present = len(faces_msg) > 0
                    face_t_ns = now_ns()
                    if present and not faces_present:
                        reacquire_ms = max(0.0, (face_t_ns - faces_absent_since_ns) / 1_000_000.0)
                        if reacquire_ms > 0.0:
                            face_reacquire_samples_ms.append(reacquire_ms)
                            if len(face_reacquire_samples_ms) > 200:
                                face_reacquire_samples_ms = face_reacquire_samples_ms[-200:]
                    if (not present) and faces_present:
                        faces_absent_since_ns = face_t_ns
                    faces_present = present
                for worker in _drain_all(q_worker):
                    module = str(worker.get("module", "") or "").strip()
                    if not module:
                        continue
                    stats["worker_loops"][module] = {
                        "t_ns": int(worker.get("t_ns", 0) or 0),
                        "idle_cycles": int(worker.get("idle_cycles", 0) or 0),
                        "processed_cycles": int(worker.get("processed_cycles", 0) or 0),
                    }

                now_s = time.time()
                if now_s < next_emit:
                    time.sleep(0.02)
                    continue
                next_emit = now_s + period

                bus_summary: Dict[str, Any] = {}
                if hasattr(bus, "get_drop_counts") and hasattr(bus, "get_publish_counts"):
                    try:
                        drop_counts = dict(bus.get_drop_counts())
                        publish_counts = dict(bus.get_publish_counts())
                        delta_drop = {
                            topic: int(drop_counts.get(topic, 0) - prev_drop_counts.get(topic, 0))
                            for topic in set(drop_counts.keys()) | set(prev_drop_counts.keys())
                        }
                        delta_publish = {
                            topic: int(publish_counts.get(topic, 0) - prev_publish_counts.get(topic, 0))
                            for topic in set(publish_counts.keys()) | set(prev_publish_counts.keys())
                        }
                        prev_drop_counts = drop_counts
                        prev_publish_counts = publish_counts
                        bus_summary = {
                            "total_drops": int(sum(drop_counts.values())),
                            "audio_drops_total": int(
                                sum(value for topic, value in drop_counts.items() if str(topic).startswith("audio."))
                            ),
                            "drop_delta": delta_drop,
                            "publish_delta": delta_publish,
                        }
                    except Exception:
                        bus_summary = {}

                snapshot = {
                    "t_ns": now_ns(),
                    "audio_frames": dict(stats["audio_frames"]),
                    "enhanced_final": dict(stats["enhanced_final"]),
                    "audio_capture": dict(stats["audio_capture"]),
                    "worker_loops": {module: dict(values) for module, values in stats["worker_loops"].items()},
                    "fusion_debug": {},
                    "vision_debug": {},
                    "bus": bus_summary,
                    "bus_drop_counts_window": dict(bus_summary.get("drop_delta", {})) if isinstance(bus_summary, dict) else {},
                }
                running_elapsed_ns = max(0, snapshot["t_ns"] - lock_state_since_ns)
                running_lock_ns = lock_dwell_ns
                running_fallback_ns = fallback_dwell_ns
                if lock_state == "NO_LOCK":
                    running_fallback_ns += running_elapsed_ns
                else:
                    running_lock_ns += running_elapsed_ns
                snapshot["fusion_debug"] = {
                    "interruptions_committed": int(interruption_count),
                    "lock_dwell_ms": float(running_lock_ns / 1_000_000.0),
                    "fallback_dwell_ms": float(running_fallback_ns / 1_000_000.0),
                }
                snapshot["vision_debug"] = {
                    "face_reacquire_ms_p50": _percentile(face_reacquire_samples_ms, 50.0),
                    "face_reacquire_ms_p95": _percentile(face_reacquire_samples_ms, 95.0),
                }
                bus.publish("runtime.perf", snapshot)
                if fh is not None:
                    fh.write(json.dumps(snapshot, sort_keys=True) + "\n")
                    fh.flush()
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "core.perf_monitor", "perf_failed", {"error": str(exc)})
        finally:
            try:
                if fh is not None:
                    fh.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_run, name="perf", daemon=True)
    thread.start()
    return thread
