"""
CONTRACT: docs/11_contract_index.md
ROLE: Orchestration entrypoint for the FocusField pipeline.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: log.events  Type: LogEvent

CONFIG KEYS:
  - runtime.mode: selected run mode (mvp/full/bench/replay)
  - runtime.config_path: path to YAML config

PERF / TIMING:
  - start modules in defined order; stop in reverse order

FAILURE MODES:
  - module start failure -> stop pipeline -> log module_failed

LOG EVENTS:
  - module=main.run, event=module_failed, payload keys=module, error

TESTS:
  - tests/contract_tests.md must cover startup invariants
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from focusfield.core.bus import Bus
from focusfield.core.config import load_config
from focusfield.core.clock import now_ns
from focusfield.core.artifacts import create_run_dir, write_run_metadata
from focusfield.core.health import start_health_monitor
from focusfield.core.log_sink import start_log_sink
from focusfield.core.logging import LogEmitter
from focusfield.audio.capture import start_audio_capture
from focusfield.audio.beamform.delay_and_sum import start_delay_and_sum
from focusfield.audio.beamform.mvdr import start_mvdr
from focusfield.audio.doa.srp_phat import start_srp_phat
from focusfield.audio.enhance.denoise import start_denoise
from focusfield.audio.output.sink import start_output_sink
from focusfield.audio.vad import start_audio_vad
from focusfield.bench.replay.recorder import start_trace_recorder
from focusfield.core.perf_monitor import start_perf_monitor
from focusfield.audio.sync.drift_check import start_drift_check
from focusfield.fusion.av_association import start_av_association
from focusfield.fusion.lock_state_machine import start_lock_state_machine
from focusfield.ui.server import start_ui_server
from focusfield.ui.telemetry import start_telemetry
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking


def _start_beamformed_passthrough(bus: Bus, logger: LogEmitter, stop_event: threading.Event) -> threading.Thread:
    """Republish beamformed audio to the final topic when denoise is disabled."""

    q = bus.subscribe("audio.enhanced.beamformed")

    def _run() -> None:
        while not stop_event.is_set():
            try:
                msg = q.get(timeout=0.1)
            except queue.Empty:
                continue
            bus.publish("audio.enhanced.final", msg)

    thread = threading.Thread(target=_run, name="enhanced-passthrough", daemon=True)
    thread.start()
    logger.emit("info", "main.run", "denoise_disabled_passthrough", {})
    return thread


def _ensure_artifacts(config: Dict[str, Any]) -> Path:
    runtime = config.setdefault("runtime", {})
    artifacts_cfg = runtime.setdefault("artifacts", {})
    base_dir = str(artifacts_cfg.get("dir", "artifacts"))
    retention = artifacts_cfg.get("retention", {})
    if not isinstance(retention, dict):
        retention = {}
    max_runs = int(retention.get("max_runs", 10))
    run_id = str(runtime.get("run_id", "") or "")
    run_dir = create_run_dir(base_dir, run_id=run_id, max_runs=max_runs)
    runtime["run_id"] = run_dir.name
    artifacts_cfg["dir"] = base_dir
    artifacts_cfg["dir_run"] = str(run_dir)
    write_run_metadata(run_dir, config)
    return run_dir


def _install_crash_handlers(
    bus: Bus,
    run_dir: Path,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
) -> tuple[threading.Event, Dict[str, Any]]:
    fail_fast = bool(config.get("runtime", {}).get("fail_fast", True))
    crash_event = threading.Event()
    crash_info: Dict[str, Any] = {}
    state_cache: Dict[str, Any] = {}
    cache_lock = threading.Lock()

    topics = [
        "runtime.health",
        "fusion.target_lock",
        "audio.vad",
        "audio.doa_heatmap",
        "vision.face_tracks",
        "audio.beamformer.debug",
        "runtime.perf",
    ]
    queues = {topic: bus.subscribe(topic) for topic in topics}

    def _cache_worker() -> None:
        while not stop_event.is_set():
            for topic, q in queues.items():
                try:
                    while True:
                        msg = q.get_nowait()
                        with cache_lock:
                            state_cache[topic] = msg
                except queue.Empty:
                    continue
                except Exception:
                    continue
            time.sleep(0.02)

    threading.Thread(target=_cache_worker, name="crash-state-cache", daemon=True).start()

    def _write_crash_report(exc_type: type[BaseException], exc: BaseException, tb, thread_name: str) -> None:
        crash_dir = run_dir / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        path = crash_dir / "crash.json"
        with cache_lock:
            cached = dict(state_cache)
        payload = {
            "t_ns": now_ns(),
            "thread": thread_name,
            "exception": {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc),
                "traceback": traceback.format_exception(exc_type, exc, tb),
            },
            "runtime": {
                "run_id": config.get("runtime", {}).get("run_id"),
                "fail_fast": fail_fast,
            },
            "last_state": cached,
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as write_exc:  # noqa: BLE001
            print(f"Failed to write crash report: {write_exc}", file=sys.stderr)

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        crash_info.clear()
        crash_info.update(
            {
                "thread": getattr(args.thread, "name", "<unknown>"),
                "type": getattr(args.exc_type, "__name__", str(args.exc_type)),
                "message": str(args.exc_value),
            }
        )
        _write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, crash_info["thread"])
        try:
            logger.emit(
                "error",
                "main.run",
                "thread_crash",
                {"thread": crash_info["thread"], "type": crash_info["type"], "message": crash_info["message"]},
            )
        except Exception:
            pass
        crash_event.set()
        stop_event.set()
        if fail_fast:
            return

    threading.excepthook = _thread_excepthook

    def _sys_excepthook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        _write_crash_report(exc_type, exc, tb, "main")
        try:
            logger.emit(
                "error",
                "main.run",
                "crash",
                {"thread": "main", "type": getattr(exc_type, "__name__", str(exc_type)), "message": str(exc)},
            )
        except Exception:
            pass
        crash_event.set()
        stop_event.set()
        if fail_fast:
            raise SystemExit(1)

    sys.excepthook = _sys_excepthook
    return crash_event, crash_info


def main() -> None:
    parser = argparse.ArgumentParser(description="FocusField vision-first runner")
    parser.add_argument("--config", default="configs/mvp_1cam_4mic.yaml", help="Path to YAML config")
    parser.add_argument("--mode", default="vision", help="Run mode (vision)")
    args = parser.parse_args()

    config = load_config(args.config)
    run_dir = _ensure_artifacts(config)
    bus = Bus(max_queue_depth=int(config.get("bus", {}).get("max_queue_depth", 8)))
    logger = LogEmitter(bus, min_level=config.get("logging", {}).get("level", "info"), run_id=str(config.get("runtime", {}).get("run_id", "")))
    stop_event = threading.Event()

    drop_throttle: Dict[str, float] = {}

    def _on_drop(topic: str, depth: int) -> None:
        now_s = time.time()
        last = drop_throttle.get(topic, 0.0)
        if now_s - last < 0.25:
            return
        drop_throttle[topic] = now_s
        if topic == "log.events":
            print(json.dumps({"t_ns": now_ns(), "level": "warning", "module": "core.bus", "event": "queue_full", "topic": topic, "depth": depth}), file=sys.stderr)
            return
        logger.emit("warning", "core.bus", "queue_full", {"topic": topic, "depth": depth})

    bus.set_drop_handler(_on_drop)

    crash_event, crash_info = _install_crash_handlers(bus, run_dir, config, logger, stop_event)

    threads: List[threading.Thread] = []
    if args.mode not in {"vision"}:
        logger.emit("error", "main.run", "invalid_mode", {"mode": args.mode})
        raise SystemExit(f"Unsupported mode: {args.mode}")

    log_thread = start_log_sink(bus, config, logger, stop_event)
    if log_thread is not None:
        threads.append(log_thread)

    health_thread = start_health_monitor(bus, config, logger, stop_event)
    if health_thread is not None:
        threads.append(health_thread)

    perf_thread = start_perf_monitor(bus, config, logger, stop_event)
    if perf_thread is not None:
        threads.append(perf_thread)

    drift_thread = start_drift_check(bus, config, logger, stop_event)
    if drift_thread is not None:
        threads.append(drift_thread)

    audio_thread = start_audio_capture(bus, config, logger, stop_event)
    if audio_thread is not None:
        threads.append(audio_thread)
    vad_thread = start_audio_vad(bus, config, logger, stop_event)
    if vad_thread is not None:
        threads.append(vad_thread)
    doa_thread = start_srp_phat(bus, config, logger, stop_event)
    if doa_thread is not None:
        threads.append(doa_thread)

    threads.extend(start_cameras(bus, config, logger, stop_event))
    threads.append(start_face_tracking(bus, config, logger, stop_event))
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    threads.append(start_av_association(bus, config, logger, stop_event))
    threads.append(start_lock_state_machine(bus, config, logger, stop_event))

    beam_thread = start_mvdr(bus, config, logger, stop_event)
    if beam_thread is None:
        beam_thread = start_delay_and_sum(bus, config, logger, stop_event)
    if beam_thread is not None:
        threads.append(beam_thread)

    denoise_thread = start_denoise(bus, config, logger, stop_event)
    if denoise_thread is not None:
        threads.append(denoise_thread)
    else:
        threads.append(_start_beamformed_passthrough(bus, logger, stop_event))

    sink_thread = start_output_sink(bus, config, logger, stop_event)
    if sink_thread is not None:
        threads.append(sink_thread)

    trace_thread = start_trace_recorder(bus, config, logger, stop_event)
    if trace_thread is not None:
        threads.append(trace_thread)
    threads.append(start_telemetry(bus, config, logger, stop_event))
    threads.append(start_ui_server(bus, config, logger, stop_event))

    logger.emit("info", "main.run", "started", {"mode": args.mode})
    try:
        while not stop_event.is_set() and not crash_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.emit("info", "main.run", "shutdown", {})
        stop_event.set()
    except Exception:
        stop_event.set()
        raise

    if crash_event.is_set() and bool(config.get("runtime", {}).get("fail_fast", True)):
        logger.emit("error", "main.run", "crashed", crash_info)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
