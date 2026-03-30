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
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_output = bus.subscribe("audio.output.stats")

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
        "audio_output": {
            "t_ns": 0,
            "sink": None,
            "backend": None,
            "occupancy_frames": 0,
            "target_buffer_frames": 0,
            "buffer_capacity_frames": 0,
            "underrun_window": 0,
            "underrun_total": 0,
            "overrun_window": 0,
            "overrun_total": 0,
            "device_error_window": 0,
            "device_error_total": 0,
            "sample_rate_mismatch_window": 0,
            "sample_rate_mismatch_total": 0,
            "block_size_mismatch_window": 0,
            "block_size_mismatch_total": 0,
            "input_age_ms": None,
            "resample_ratio": None,
            "stage_timestamps": {},
        },
        "shed_state": {
            "active": False,
            "level": 0,
            "reason": "normal",
            "targets": [],
        },
    }
    stage_latency_history: Dict[str, List[float]] = {
        "capture_to_publish_ms": [],
        "capture_to_beamform_ms": [],
        "capture_to_denoise_ms": [],
        "capture_to_output_ms": [],
    }
    output_occupancy_ratios: List[float] = []
    output_underrun_windows: List[float] = []
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
    beam_fallback_active = False
    beam_fallback_windows = 0
    last_queue_pressure_warning_s = 0.0
    last_fallback_warning_s = 0.0
    last_shed_state_level = -1

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
        nonlocal beam_fallback_active
        nonlocal beam_fallback_windows
        nonlocal last_queue_pressure_warning_s
        nonlocal last_fallback_warning_s
        nonlocal last_shed_state_level
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
                    stage_latencies = _stage_latency_ms(
                        f.get("stage_timestamps", {}) if isinstance(f.get("stage_timestamps"), dict) else {},
                        t_ns,
                    )
                    _append_stage_latency_samples(stage_latency_history, stage_latencies)
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
                beam_msg = _drain_latest(q_beam)
                if isinstance(beam_msg, dict):
                    beam_fallback_active = bool(beam_msg.get("fallback_active", False))
                output_msg = _drain_latest(q_output)
                if isinstance(output_msg, dict):
                    output_stage_latencies = _stage_latency_ms(
                        output_msg.get("stage_timestamps", {})
                        if isinstance(output_msg.get("stage_timestamps"), dict)
                        else {},
                        output_msg.get("t_ns", 0),
                    )
                    _append_stage_latency_samples(stage_latency_history, output_stage_latencies)
                    occupancy_frames = int(output_msg.get("occupancy_frames", 0) or 0)
                    buffer_capacity_frames = int(output_msg.get("buffer_capacity_frames", 0) or 0)
                    occupancy_ratio = (
                        float(occupancy_frames / buffer_capacity_frames)
                        if buffer_capacity_frames > 0
                        else None
                    )
                    if occupancy_ratio is not None:
                        output_occupancy_ratios.append(max(0.0, occupancy_ratio))
                        if len(output_occupancy_ratios) > 300:
                            del output_occupancy_ratios[:-300]
                    underrun_window = int(output_msg.get("underrun_window", 0) or 0)
                    output_underrun_windows.append(float(underrun_window))
                    if len(output_underrun_windows) > 300:
                        del output_underrun_windows[:-300]
                    stats["audio_output"] = {
                        "t_ns": int(output_msg.get("t_ns", 0) or 0),
                        "sink": output_msg.get("sink"),
                        "backend": output_msg.get("backend"),
                        "device_name": output_msg.get("device_name"),
                        "occupancy_frames": occupancy_frames,
                        "target_buffer_frames": int(output_msg.get("target_buffer_frames", 0) or 0),
                        "buffer_capacity_frames": int(output_msg.get("buffer_capacity_frames", 0) or 0),
                        "underrun_window": underrun_window,
                        "underrun_total": int(output_msg.get("underrun_total", 0) or 0),
                        "overrun_window": int(output_msg.get("overrun_window", 0) or 0),
                        "overrun_total": int(output_msg.get("overrun_total", 0) or 0),
                        "device_error_window": int(output_msg.get("device_error_window", 0) or 0),
                        "device_error_total": int(output_msg.get("device_error_total", 0) or 0),
                        "sample_rate_mismatch_window": int(output_msg.get("sample_rate_mismatch_window", 0) or 0),
                        "sample_rate_mismatch_total": int(output_msg.get("sample_rate_mismatch_total", 0) or 0),
                        "block_size_mismatch_window": int(output_msg.get("block_size_mismatch_window", 0) or 0),
                        "block_size_mismatch_total": int(output_msg.get("block_size_mismatch_total", 0) or 0),
                        "input_age_ms": output_msg.get("input_age_ms"),
                        "resample_ratio": output_msg.get("resample_ratio"),
                        "occupancy_ratio": occupancy_ratio,
                        "stage_timestamps": dict(output_msg.get("stage_timestamps", {}))
                        if isinstance(output_msg.get("stage_timestamps"), dict)
                        else {},
                    }
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
                    "beamformer": {"fallback_active": bool(beam_fallback_active)},
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
                snapshot["audio_output"] = dict(stats["audio_output"])
                snapshot["stage_latency_ms"] = _stage_latency_ms(
                    snapshot["audio_output"].get("stage_timestamps", {}),
                    snapshot["audio_output"].get("t_ns", 0),
                )
                snapshot["stage_latency_rolling_ms"] = _rolling_stage_latency(stage_latency_history)
                snapshot["audio_output_rolling"] = {
                    "occupancy_ratio_p50": _percentile(output_occupancy_ratios, 50.0),
                    "occupancy_ratio_p95": _percentile(output_occupancy_ratios, 95.0),
                    "occupancy_ratio_p99": _percentile(output_occupancy_ratios, 99.0),
                    "underrun_window_p50": _percentile(output_underrun_windows, 50.0),
                    "underrun_window_p95": _percentile(output_underrun_windows, 95.0),
                    "underrun_window_p99": _percentile(output_underrun_windows, 99.0),
                }
                shed_state = _derive_shed_state(snapshot)
                snapshot["shed_state"] = shed_state
                if int(shed_state.get("level", 0) or 0) != last_shed_state_level:
                    last_shed_state_level = int(shed_state.get("level", 0) or 0)
                    bus.publish("runtime.shed_state", shed_state)
                queue_drop_total = int(sum(snapshot["bus_drop_counts_window"].values()))
                capture_overflow_window = int(
                    snapshot["audio_capture"].get("status_input_overflow_window", 0) or 0
                )
                snapshot["queue_pressure"] = {
                    "drop_total_window": queue_drop_total,
                    "capture_overflow_window": capture_overflow_window,
                }
                if beam_fallback_active:
                    beam_fallback_windows += 1
                else:
                    beam_fallback_windows = 0
                if (queue_drop_total > 0 or capture_overflow_window > 0) and (now_s - last_queue_pressure_warning_s) >= 5.0:
                    logger.emit(
                        "warning",
                        "core.perf_monitor",
                        "queue_pressure",
                        {
                            "drop_total_window": queue_drop_total,
                            "capture_overflow_window": capture_overflow_window,
                            "drop_counts_window": snapshot["bus_drop_counts_window"],
                        },
                    )
                    last_queue_pressure_warning_s = now_s
                if beam_fallback_windows >= 3 and (now_s - last_fallback_warning_s) >= 5.0:
                    logger.emit(
                        "warning",
                        "core.perf_monitor",
                        "fallback_persistent",
                        {
                            "windows": beam_fallback_windows,
                            "emit_hz": emit_hz,
                            "fallback_active": True,
                        },
                    )
                    last_fallback_warning_s = now_s
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


def _stage_latency_ms(stage_timestamps: Dict[str, Any], sink_t_ns: Any) -> Dict[str, Optional[float]]:
    if not isinstance(stage_timestamps, dict):
        return {}
    captured_t_ns = _as_int(stage_timestamps.get("captured_t_ns"))
    published_t_ns = _as_int(stage_timestamps.get("published_t_ns"))
    beamformed_t_ns = _as_int(stage_timestamps.get("beamformed_t_ns"))
    denoised_t_ns = _as_int(stage_timestamps.get("denoised_t_ns"))
    sink_t = _as_int(sink_t_ns)
    latencies: Dict[str, Optional[float]] = {}
    if captured_t_ns is None:
        return latencies
    if published_t_ns is not None:
        latencies["capture_to_publish_ms"] = max(0.0, (published_t_ns - captured_t_ns) / 1_000_000.0)
    if beamformed_t_ns is not None:
        latencies["capture_to_beamform_ms"] = max(0.0, (beamformed_t_ns - captured_t_ns) / 1_000_000.0)
    if denoised_t_ns is not None:
        latencies["capture_to_denoise_ms"] = max(0.0, (denoised_t_ns - captured_t_ns) / 1_000_000.0)
    if sink_t is not None:
        latencies["capture_to_output_ms"] = max(0.0, (sink_t - captured_t_ns) / 1_000_000.0)
    return latencies


def _append_stage_latency_samples(history: Dict[str, List[float]], latencies: Dict[str, Optional[float]]) -> None:
    for key, value in latencies.items():
        if value is None:
            continue
        samples = history.setdefault(key, [])
        samples.append(float(value))
        if len(samples) > 300:
            del samples[:-300]


def _rolling_stage_latency(history: Dict[str, List[float]]) -> Dict[str, Dict[str, Optional[float]]]:
    summary: Dict[str, Dict[str, Optional[float]]] = {}
    for key, samples in history.items():
        summary[key] = {
            "count": float(len(samples)),
            "p50_ms": _percentile(samples, 50.0),
            "p95_ms": _percentile(samples, 95.0),
            "p99_ms": _percentile(samples, 99.0),
        }
    return summary


def _derive_shed_state(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    queue_pressure = snapshot.get("queue_pressure") if isinstance(snapshot.get("queue_pressure"), dict) else {}
    audio_output = snapshot.get("audio_output") if isinstance(snapshot.get("audio_output"), dict) else {}
    queue_drop_total = int((queue_pressure or {}).get("drop_total_window", 0) or 0)
    capture_overflow = int((queue_pressure or {}).get("capture_overflow_window", 0) or 0)
    occupancy_ratio = _as_float((audio_output or {}).get("occupancy_ratio"))
    underrun_window = int((audio_output or {}).get("underrun_window", 0) or 0)
    underrun_total = int((audio_output or {}).get("underrun_total", 0) or 0)

    level = 0
    targets: List[str] = []
    reasons: List[str] = []
    if queue_drop_total > 0 or capture_overflow > 0:
        level = max(level, 1)
        targets.extend(["ui", "telemetry"])
        reasons.append("queue_pressure")
    if queue_drop_total >= 5 or capture_overflow >= 2 or underrun_window > 0 or underrun_total > 0:
        level = max(level, 2)
        targets.extend(["vision", "denoise"])
        reasons.append("audio_backpressure")
    if (occupancy_ratio is not None and occupancy_ratio >= 0.85) or queue_drop_total >= 20:
        level = max(level, 3)
        targets.extend(["noncritical"])
        reasons.append("sustained_overload")

    deduped_targets = []
    for item in targets:
        if item not in deduped_targets:
            deduped_targets.append(item)
    reason = ",".join(dict.fromkeys(reasons)) if reasons else "normal"
    return {
        "active": bool(level > 0),
        "level": int(level),
        "reason": reason,
        "targets": deduped_targets,
        "queue_pressure": {
            "drop_total_window": queue_drop_total,
            "capture_overflow_window": capture_overflow,
        },
        "output_occupancy_ratio": occupancy_ratio,
        "output_underrun_window": underrun_window,
        "output_underrun_total": underrun_total,
    }


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:  # noqa: BLE001
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None
