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
from typing import Any, Dict, Optional

from focusfield.core.clock import now_ns


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

    stats: Dict[str, Any] = {
        "audio_frames": {"last_t_ns": 0, "count": 0},
        "enhanced_final": {"last_t_ns": 0, "count": 0, "last_latency_ms": None},
        "audio_capture": {
            "queue_depth": 0,
            "frames_enqueued": 0,
            "frames_published": 0,
            "callback_overflow_drop": 0,
            "status_input_overflow": 0,
        },
    }

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _run() -> None:
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
                    # Approx latency: wall-clock now - message t_ns
                    latency_ms = (now_ns() - t_ns) / 1_000_000.0 if t_ns else None
                    stats["enhanced_final"]["last_latency_ms"] = float(latency_ms) if latency_ms is not None else None
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
                    "bus": bus_summary,
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
