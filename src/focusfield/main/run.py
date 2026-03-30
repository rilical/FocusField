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
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from focusfield.core.config import load_config
from focusfield.core.clock import now_ns
from focusfield.core.artifacts import create_run_dir, write_run_metadata
from focusfield.core.health import start_health_monitor
from focusfield.core.log_sink import start_log_sink
from focusfield.core.logging import LogEmitter
from focusfield.audio.capture import start_audio_capture
from focusfield.audio.devices import is_raw_array_device, list_input_devices, resolve_input_device_index
from focusfield.audio.beamform.delay_and_sum import start_delay_and_sum
from focusfield.audio.beamform.mvdr import start_mvdr
from focusfield.audio.doa.srp_phat import start_srp_phat
from focusfield.audio.enhance.denoise import start_denoise
from focusfield.audio.fft_backend import backend_name as fft_backend_name
from focusfield.audio.mic_health import start_audio_mic_health
from focusfield.audio.output.sink import start_output_sink
from focusfield.audio.vad import start_audio_vad
from focusfield.bench.replay.recorder import start_trace_recorder
from focusfield.core.perf_monitor import start_perf_monitor
from focusfield.audio.sync.drift_check import start_drift_check
from focusfield.fusion.av_association import start_av_association
from focusfield.fusion.lock_state_machine import start_lock_state_machine
from focusfield.uma8.led_control import start_uma8_led_service
from focusfield.ui.server import start_ui_server
from focusfield.ui.telemetry import start_telemetry
from focusfield.platform.hardware_probe import normalize_camera_scope, try_open_camera_any_backend
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking
from focusfield.main.runtime_multiprocess import start_multiprocess_runtime
from focusfield.main.modes import KNOWN_RUNTIME_MODES, normalize_runtime_mode
from focusfield.main.runtime_support import (
    apply_runtime_os_tuning,
    apply_runtime_thread_caps,
    build_bus,
    camera_topics,
    runtime_process_mode,
    runtime_requirements,
    start_beamformed_passthrough,
)


def _selected_audio_info(config: Dict[str, Any]) -> Dict[str, Any]:
    selected_idx = resolve_input_device_index(config, logger=None)
    selected_name = ""
    selected_channels = 0
    for device in list_input_devices():
        if device.index != selected_idx:
            continue
        selected_name = device.name
        selected_channels = int(device.max_input_channels)
        break
    return {
        "device_index": selected_idx,
        "device_name": selected_name,
        "channels": selected_channels,
    }


def _configured_camera_bindings(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    cameras = config.get("video", {}).get("cameras", [])
    if not isinstance(cameras, list):
        cameras = []
    bindings: List[Dict[str, Any]] = []
    for idx, cam in enumerate(cameras):
        if not isinstance(cam, dict):
            continue
        bindings.append(
            {
                "camera_id": str(cam.get("id", f"cam{idx}")),
                "device_path": str(cam.get("device_path", "") or ""),
                "device_index": cam.get("device_index"),
                "yaw_offset_deg": float(cam.get("yaw_offset_deg", 0.0) or 0.0),
                "bearing_offset_deg": float(cam.get("bearing_offset_deg", 0.0) or 0.0),
                "hfov_deg": float(cam.get("hfov_deg", 0.0) or 0.0),
            }
        )
    return bindings


def _configured_camera_status(config: Dict[str, Any], strict_capture: bool, camera_scope: str) -> Dict[str, Any]:
    cameras = config.get("video", {}).get("cameras", [])
    if not isinstance(cameras, list):
        cameras = []
    total = len(cameras)
    openable = 0
    entries: List[Dict[str, Any]] = []

    def _camera_source(cam: Any, idx: int) -> Any:
        if not isinstance(cam, dict):
            return idx
        path = cam.get("device_path")
        if isinstance(path, str) and path.strip():
            return path
        cam_index = cam.get("device_index")
        if isinstance(cam_index, int):
            return cam_index
        if isinstance(cam_index, str):
            try:
                return int(cam_index)
            except (TypeError, ValueError):
                return idx
        return idx

    def _open_camera(source: Any) -> tuple[bool, list[tuple[object, str]], tuple[object, str] | None]:
        if not isinstance(source, str):
            return try_open_camera_any_backend(
                source,
                strict_capture=strict_capture,
                camera_scope=camera_scope,
            )
        ok, tried, opened = try_open_camera_any_backend(
            source,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
        if ok or not strict_capture:
            return ok, tried, opened
        fallback_ok, fallback_tried, fallback_opened = try_open_camera_any_backend(
            source,
            strict_capture=False,
            camera_scope=camera_scope,
        )
        if fallback_ok:
            return fallback_ok, tried + fallback_tried, fallback_opened
        return False, tried, opened

    for idx, cam in enumerate(cameras):
        source: object = _camera_source(cam, idx)
        camera_id = f"cam{idx}"
        if isinstance(cam, dict):
            camera_id = str(cam.get("id", camera_id))
        ok, tried, opened = _open_camera(source)
        if ok:
            openable += 1
        entries.append(
            {
                "camera_id": camera_id,
                "source": source,
                "open": ok,
                "backend": opened[1] if opened is not None else "none",
                "tried": tried,
                "camera_scope": camera_scope,
            }
        )
    return {
        "total": total,
        "openable": openable,
        "entries": entries,
    }


def _uma8_mode_hint(name: str, channels: int, required: int) -> str:
    if not is_raw_array_device(name):
        return ""
    if channels >= required:
        return ""
    if required < 8:
        return ""
    return " hint: miniDSP UMA-8 appears in 2ch DSP mode; switch to RAW firmware for 8ch."


def _validate_runtime_requirements(config: Dict[str, Any], logger: LogEmitter) -> None:
    req = runtime_requirements(config)
    if not req["strict"]:
        return

    failures: List[str] = []
    camera_scope = str(req["camera_scope"])
    camera_status = _configured_camera_status(config, strict_capture=True, camera_scope=camera_scope)
    audio_status = _selected_audio_info(config)
    min_cameras = int(req["min_cameras"])
    min_audio_channels = int(req["min_audio_channels"])

    if min_cameras > 0 and camera_status["openable"] < min_cameras:
        failures.append(
            f"required cameras={min_cameras}, observed openable cameras={camera_status['openable']} "
            f"(camera_scope={camera_scope})"
        )
    if min_audio_channels > 0 and int(audio_status["channels"]) < min_audio_channels:
        hint = _uma8_mode_hint(str(audio_status.get("device_name", "")), int(audio_status["channels"]), min_audio_channels)
        failures.append(
            f"required audio_channels={min_audio_channels}, observed channels={audio_status['channels']} "
            f"(index={audio_status.get('device_index')}, name={audio_status.get('device_name')!r}){hint}"
        )
    if not failures:
        logger.emit(
            "info",
            "main.run",
            "runtime_requirements_passed",
            {
                "requirements": req,
                "camera_status": {"total": camera_status["total"], "openable": camera_status["openable"]},
                "audio_status": audio_status,
            },
        )
        return

    payload = {
        "requirements": req,
        "camera_status": camera_status,
        "audio_status": audio_status,
        "failures": failures,
    }
    logger.emit("error", "main.run", "runtime_requirements_failed", payload)
    joined = "\n".join(f"- {item}" for item in failures)
    raise RuntimeError(f"Runtime requirements check failed:\n{joined}")


def _validate_face_detector_requirements(
    config: Dict[str, Any],
    logger: LogEmitter,
    req: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_req = req or runtime_requirements(config)
    strict = bool(runtime_req.get("strict", False))
    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    face_cfg = vision_cfg.get("face", {})
    if not isinstance(face_cfg, dict):
        face_cfg = {}
    requested = str(face_cfg.get("backend", face_cfg.get("detector_backend", "auto")) or "auto").strip().lower()
    has_yunet = False
    has_haar = False
    try:
        import cv2  # type: ignore

        has_yunet = bool(
            hasattr(cv2, "FaceDetectorYN_create")
            or (
                hasattr(cv2, "FaceDetectorYN")
                and hasattr(cv2.FaceDetectorYN, "create")
            )
        )
        haar_path = getattr(cv2.data, "haarcascades", "") + "haarcascade_frontalface_default.xml"
        has_haar = bool(haar_path) and os.path.exists(haar_path)
    except Exception:
        has_yunet = False
        has_haar = False

    active = requested
    degraded = False
    operational = True
    reason = ""
    if requested == "auto":
        if has_yunet:
            active = "yunet"
        elif has_haar:
            active = "haar"
        else:
            active = "none"
            operational = False
            reason = "no face detector backend available"
    elif requested == "yunet":
        if has_yunet:
            active = "yunet"
        elif has_haar:
            active = "haar"
            degraded = True
            reason = "yunet unavailable; fell back to haar"
        else:
            active = "none"
            operational = False
            reason = "yunet unavailable and haar missing"
    elif requested == "haar":
        if has_haar:
            active = "haar"
        else:
            active = "none"
            operational = False
            reason = "haar cascade missing"
    else:
        active = requested
        degraded = True
        reason = f"unknown detector backend '{requested}'"

    cameras = config.get("video", {}).get("cameras", [])
    if not isinstance(cameras, list):
        cameras = []
    per_camera = []
    for idx, cam_cfg in enumerate(cameras):
        camera_id = f"cam{idx}"
        path = None
        if isinstance(cam_cfg, dict):
            camera_id = str(cam_cfg.get("id", camera_id))
            path = cam_cfg.get("device_path") or cam_cfg.get("device")
        per_camera.append(
            {
                "camera_id": camera_id,
                "path": path,
                "requested_backend": requested,
                "active_backend": active,
                "operational": operational,
                "degraded": degraded,
            }
        )

    payload = {
        "requested_backend": requested,
        "active_backend": active,
        "operational": operational,
        "degraded": degraded,
        "reason": reason,
        "haar_available": has_haar,
        "yunet_available": has_yunet,
        "per_camera_active_backend": per_camera,
    }
    if not operational:
        logger.emit("error", "main.run", "face_detector_requirements_failed", payload)
        if strict:
            raise RuntimeError(reason or "No operational face detector backend available")
    elif degraded:
        logger.emit("warning", "main.run", "face_detector_degraded", payload)
    else:
        logger.emit("info", "main.run", "face_detector_ready", payload)
    return payload


def _validate_led_hid_runtime(
    config: Dict[str, Any],
    logger: LogEmitter,
    req: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_req = req or runtime_requirements(config)
    required = bool(runtime_req.get("require_led_hid_runtime_check", False))
    payload = {"required": required, "ok": True, "reason": ""}
    if not required:
        return payload
    helper = Path("scripts/led_ring_hid.py")
    if not helper.exists():
        payload["ok"] = False
        payload["reason"] = "scripts/led_ring_hid.py missing"
        logger.emit("error", "main.run", "led_hid_runtime_failed", payload)
        if bool(runtime_req.get("strict", False)):
            raise RuntimeError(str(payload["reason"]))
    return payload


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


def _configure_runtime_environment(config: Dict[str, Any]) -> None:
    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    vision_models = vision_cfg.get("models", {})
    if not isinstance(vision_models, dict):
        vision_models = {}
    audio_models = audio_cfg.get("models", {})
    if not isinstance(audio_models, dict):
        audio_models = {}
    allow_downloads = bool(vision_models.get("allow_runtime_downloads", True)) and bool(
        audio_models.get("allow_runtime_downloads", True)
    )
    os.environ["FOCUSFIELD_ALLOW_RUNTIME_DOWNLOADS"] = "1" if allow_downloads else "0"


def _runtime_startup_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    startup_cfg = config.get("runtime", {}).get("startup", {})
    if not isinstance(startup_cfg, dict):
        startup_cfg = {}
    return startup_cfg


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
        "audio.mic_health",
        "audio.doa_heatmap",
        "vision.face_tracks",
        "audio.beamformer.debug",
        "runtime.perf",
        "audio.capture.stats",
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


def _start_parent_modules(
    bus: Any,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []

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

    threads.append(start_av_association(bus, config, logger, stop_event))
    threads.append(start_lock_state_machine(bus, config, logger, stop_event))
    threads.append(start_uma8_led_service(bus, config, logger, stop_event))

    sink_thread = start_output_sink(bus, config, logger, stop_event)
    if sink_thread is not None:
        threads.append(sink_thread)

    trace_thread = start_trace_recorder(bus, config, logger, stop_event)
    if trace_thread is not None:
        threads.append(trace_thread)
    telemetry_thread = start_telemetry(bus, config, logger, stop_event)
    if telemetry_thread is not None:
        threads.append(telemetry_thread)
    ui_thread = start_ui_server(bus, config, logger, stop_event)
    if ui_thread is not None:
        threads.append(ui_thread)
    return threads


def _start_audio_threaded_workers(
    bus: Any,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []

    audio_thread = start_audio_capture(bus, config, logger, stop_event)
    if audio_thread is not None:
        threads.append(audio_thread)
    mic_health_thread = start_audio_mic_health(bus, config, logger, stop_event)
    if mic_health_thread is not None:
        threads.append(mic_health_thread)
    vad_thread = start_audio_vad(bus, config, logger, stop_event)
    if vad_thread is not None:
        threads.append(vad_thread)
    doa_thread = start_srp_phat(bus, config, logger, stop_event)
    if doa_thread is not None:
        threads.append(doa_thread)
    beam_thread = start_mvdr(bus, config, logger, stop_event)
    if beam_thread is None:
        beam_thread = start_delay_and_sum(bus, config, logger, stop_event)
    if beam_thread is not None:
        threads.append(beam_thread)

    denoise_thread = start_denoise(bus, config, logger, stop_event)
    if denoise_thread is not None:
        threads.append(denoise_thread)
    else:
        threads.append(start_beamformed_passthrough(bus, logger, stop_event, "main.run"))
    return threads


def _start_vision_threaded_workers(
    bus: Any,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
    req: Dict[str, Any],
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []

    threads.extend(
        start_cameras(
            bus,
            config,
            logger,
            stop_event,
            strict_capture=req["strict"],
            camera_scope=req["camera_scope"],
        )
    )
    threads.append(start_face_tracking(bus, config, logger, stop_event))
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    return threads


def _start_threaded_workers(
    bus: Any,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
    req: Dict[str, Any],
) -> List[threading.Thread]:
    threads = _start_audio_threaded_workers(bus, config, logger, stop_event)
    threads.extend(_start_vision_threaded_workers(bus, config, logger, stop_event, req))
    return threads


def _start_threaded_runtime_staged(
    bus: Any,
    config: Dict[str, Any],
    logger: LogEmitter,
    stop_event: threading.Event,
    req: Dict[str, Any],
) -> List[threading.Thread]:
    threads = _start_audio_threaded_workers(bus, config, logger, stop_event)
    startup_cfg = _runtime_startup_cfg(config)
    vision_delay_ms = max(0, int(startup_cfg.get("vision_start_delay_ms", 0) or 0))

    def _launch_vision() -> None:
        if vision_delay_ms > 0:
            time.sleep(vision_delay_ms / 1000.0)
        if stop_event.is_set():
            return
        _start_vision_threaded_workers(bus, config, logger, stop_event, req)
        logger.emit(
            "info",
            "main.run",
            "vision_workers_started",
            {
                "delay_ms": vision_delay_ms,
                "mode": str(config.get("runtime", {}).get("mode", "") or ""),
            },
        )

    launcher = threading.Thread(target=_launch_vision, name="runtime-start-vision", daemon=True)
    launcher.start()
    threads.append(launcher)
    return threads


def main() -> None:
    parser = argparse.ArgumentParser(description="FocusField vision-first runner")
    parser.add_argument("--config", default="configs/mvp_1cam_4mic.yaml", help="Path to YAML config")
    parser.add_argument("--mode", default="", help=f"Run mode ({', '.join(KNOWN_RUNTIME_MODES)})")
    args = parser.parse_args()

    config = load_config(args.config)
    runtime_cfg = config.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        config["runtime"] = runtime_cfg
    cli_mode = args.mode.strip().lower()
    if cli_mode:
        runtime_cfg["mode"] = cli_mode
    try:
        runtime_cfg["mode"] = normalize_runtime_mode(runtime_cfg.get("mode", "mac_loopback_dev"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _configure_runtime_environment(config)
    run_dir = _ensure_artifacts(config)
    bus_cfg = config.get("bus", {})
    if not isinstance(bus_cfg, dict):
        bus_cfg = {}
    topic_queue_depths = bus_cfg.get("topic_queue_depths", {})
    if not isinstance(topic_queue_depths, dict):
        topic_queue_depths = {}
    parsed_topic_depths: Dict[str, int] = {}
    for key, value in topic_queue_depths.items():
        try:
            parsed_topic_depths[str(key)] = int(value)
        except Exception:
            continue
    bus = build_bus(config)
    logger = LogEmitter(bus, min_level=config.get("logging", {}).get("level", "info"), run_id=str(config.get("runtime", {}).get("run_id", "")))
    bus_camera_topics = camera_topics(config)
    resolved_camera_queue_depths = {topic: bus.get_topic_depth(topic) for topic in bus_camera_topics}
    logger.emit(
        "info",
        "main.run",
        "bus_config",
        {
            "max_queue_depth": int(bus_cfg.get("max_queue_depth", 8)),
            "topic_queue_depths": parsed_topic_depths,
            "camera_topic_depths": resolved_camera_queue_depths,
        },
    )

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
    tuning_role = "parent" if runtime_process_mode(config) == "multiprocess" else "main"
    apply_runtime_thread_caps(config, logger, role=tuning_role)
    apply_runtime_os_tuning(config, logger, role=tuning_role)
    logger.emit(
        "info",
        "main.run",
        "runtime_backend",
        {
            "process_mode": runtime_process_mode(config),
            "fft_backend": fft_backend_name(),
        },
    )

    crash_event, crash_info = _install_crash_handlers(bus, run_dir, config, logger, stop_event)

    threads: List[threading.Thread] = []
    mode = str(runtime_cfg.get("mode", "mac_loopback_dev") or "mac_loopback_dev")
    try:
        _validate_runtime_requirements(config, logger)
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    req = runtime_requirements(config)
    try:
        detector_status = _validate_face_detector_requirements(config, logger, req)
        led_hid_status = _validate_led_hid_runtime(config, logger, req)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    runtime_cfg["selected_audio_device"] = _selected_audio_info(config)
    runtime_cfg["configured_camera_bindings"] = _configured_camera_bindings(config)
    runtime_cfg["requirements_passed"] = True
    runtime_cfg["config_effective_path"] = str(os.environ.get("FOCUSFIELD_CONFIG_EFFECTIVE", runtime_cfg.get("config_path", "")) or "")
    runtime_cfg["config_invoked_path"] = str(os.environ.get("FOCUSFIELD_CONFIG_PATH", runtime_cfg.get("config_path", "")) or "")
    runtime_cfg["process_mode"] = runtime_process_mode(config)
    runtime_cfg["perf_profile"] = str(runtime_cfg.get("perf_profile", "") or "")
    runtime_cfg["thresholds_preset_active"] = str(config.get("fusion", {}).get("thresholds_preset", "") or "")
    runtime_cfg["requirements"] = dict(config.get("runtime", {}).get("requirements", {}) or {})
    runtime_cfg["audio_device_profile"] = str(config.get("audio", {}).get("device_profile", "") or "")
    runtime_cfg["audio_yaw_offset_deg"] = float(config.get("audio", {}).get("yaw_offset_deg", 0.0) or 0.0)
    runtime_cfg["detector_backend_active"] = detector_status.get("active_backend", "unknown")
    runtime_cfg["detector_backend_degraded"] = bool(detector_status.get("degraded", False))
    runtime_cfg["detector_backend_reason"] = detector_status.get("reason", "")
    runtime_cfg["detector_backend_per_camera"] = detector_status.get("per_camera_active_backend", [])
    runtime_cfg["led_hid_runtime"] = led_hid_status
    logger.emit(
        "info",
        "main.run",
        "resolved_bindings",
        {
            "config_path": runtime_cfg.get("config_path", ""),
            "config_effective_path": runtime_cfg.get("config_effective_path", ""),
            "process_mode": runtime_cfg.get("process_mode", ""),
            "perf_profile": runtime_cfg.get("perf_profile", ""),
            "thresholds_preset": runtime_cfg.get("thresholds_preset_active", ""),
            "requirements": runtime_cfg.get("requirements", {}),
            "audio_device": runtime_cfg.get("selected_audio_device", {}),
            "audio_device_profile": runtime_cfg.get("audio_device_profile", ""),
            "audio_yaw_offset_deg": runtime_cfg.get("audio_yaw_offset_deg", 0.0),
            "camera_bindings": runtime_cfg.get("configured_camera_bindings", []),
            "camera_calibration_overlay": runtime_cfg.get("camera_calibration_overlay", {}),
        },
    )
    threads.extend(_start_parent_modules(bus, config, logger, stop_event))
    if runtime_process_mode(config) == "multiprocess":
        threads.extend(start_multiprocess_runtime(bus, config, logger, stop_event))
    else:
        startup_cfg = _runtime_startup_cfg(config)
        if bool(startup_cfg.get("audio_first", False)):
            threads.extend(_start_threaded_runtime_staged(bus, config, logger, stop_event, req))
        else:
            threads.extend(_start_threaded_workers(bus, config, logger, stop_event, req))

    logger.emit(
        "info",
        "main.run",
        "started",
        {
            "mode": mode,
            "process_mode": runtime_process_mode(config),
            "audio_first": bool(_runtime_startup_cfg(config).get("audio_first", False)),
        },
    )
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
