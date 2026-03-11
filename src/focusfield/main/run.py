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

from focusfield.core.bus import Bus
from focusfield.core.config import load_config
from focusfield.core.clock import now_ns
from focusfield.core.artifacts import create_run_dir, write_run_metadata
from focusfield.core.health import start_health_monitor
from focusfield.core.log_sink import start_log_sink
from focusfield.core.logging import LogEmitter
from focusfield.audio.capture import start_audio_capture
from focusfield.audio.devices import list_input_devices, resolve_input_device_index
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
from focusfield.uma8.led_control import start_uma8_led_service
from focusfield.ui.server import start_ui_server
from focusfield.ui.telemetry import start_telemetry
from focusfield.platform.hardware_probe import normalize_camera_scope, try_open_camera_any_backend
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking


def _resolve_face_detector_backend(config: Dict[str, Any], logger: LogEmitter) -> None:
    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        return
    face_cfg = vision_cfg.get("face", {})
    if not isinstance(face_cfg, dict):
        return
    requested = str(face_cfg.get("detector_backend", "haar") or "haar").strip().lower()
    if requested == "blazeface":
        face_cfg["detector_backend"] = "yunet"
        logger.emit(
            "info",
            "main.run",
            "detector_backend_alias",
            {"requested_backend": "blazeface", "resolved_backend": "yunet"},
        )
        requested = "yunet"
    if requested != "yunet":
        return
    try:
        import cv2  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "warning",
            "main.run",
            "detector_backend_fallback",
            {
                "requested_backend": "yunet",
                "active_backend": "unresolved",
                "reason": f"cv2_import_failed:{exc}",
                "remediation": "Install OpenCV with FaceDetectorYN support to enable YuNet.",
            },
        )
        return
    if not hasattr(cv2, "FaceDetectorYN_create"):
        logger.emit(
            "warning",
            "main.run",
            "detector_backend_fallback",
            {
                "requested_backend": "yunet",
                "active_backend": "unresolved",
                "reason": "facedetectoryn_unavailable",
                "remediation": "Use an OpenCV build with FaceDetectorYN (opencv-contrib/official wheel).",
            },
        )
        return
    logger.emit(
        "info",
        "main.run",
        "detector_backend_ready",
        {"requested_backend": "yunet", "active_backend": "yunet"},
    )


def _haar_cascade_available() -> bool:
    try:
        import cv2  # type: ignore
    except Exception:
        return False
    candidates: List[Path] = []
    if hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
        candidates.append(Path(str(cv2.data.haarcascades)) / "haarcascade_frontalface_default.xml")
    cv2_root = Path(cv2.__file__).resolve().parent if getattr(cv2, "__file__", None) else None
    if cv2_root is not None:
        candidates.extend(
            [
                cv2_root / "data" / "haarcascade_frontalface_default.xml",
                cv2_root.parent / "share" / "opencv4" / "haarcascades" / "haarcascade_frontalface_default.xml",
                Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml"),
            ]
        )
    for path in candidates:
        if not path.exists():
            continue
        try:
            cascade = cv2.CascadeClassifier(str(path))
        except Exception:
            continue
        if not cascade.empty():
            return True
    return False


def _resolve_face_detector_runtime(config: Dict[str, Any]) -> Dict[str, Any]:
    vision_cfg = config.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    face_cfg = vision_cfg.get("face", {})
    if not isinstance(face_cfg, dict):
        face_cfg = {}
    requested = str(face_cfg.get("detector_backend", "haar") or "haar").strip().lower()
    try:
        import cv2  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {
            "requested_backend": requested,
            "active_backend": "none",
            "operational": False,
            "degraded": True,
            "reason": f"cv2_import_failed:{exc}",
        }
    yunet_available = bool(hasattr(cv2, "FaceDetectorYN_create"))
    haar_available = _haar_cascade_available()
    active_backend = requested
    degraded = False
    reason = ""
    if requested == "yunet":
        if yunet_available:
            active_backend = "yunet"
        elif haar_available:
            active_backend = "haar"
            degraded = True
            reason = "facedetectoryn_unavailable"
        else:
            active_backend = "none"
            degraded = True
            reason = "no_face_detector_backend"
    elif requested == "haar":
        if haar_available:
            active_backend = "haar"
        else:
            active_backend = "none"
            degraded = True
            reason = "haar_cascade_missing"
    return {
        "requested_backend": requested,
        "active_backend": active_backend,
        "operational": bool(active_backend in {"haar", "yunet"}),
        "degraded": bool(degraded),
        "reason": reason,
        "yunet_available": yunet_available,
        "haar_available": haar_available,
    }


def _validate_face_detector_requirements(config: Dict[str, Any], logger: LogEmitter, req: Dict[str, Any]) -> None:
    status = _resolve_face_detector_runtime(config)
    runtime_cfg = config.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        config["runtime"] = runtime_cfg
    runtime_cfg["detector_backend_active"] = str(status.get("active_backend", "unknown"))
    runtime_cfg["detector_backend_degraded"] = bool(status.get("degraded", False))
    runtime_cfg["detector_backend_reason"] = str(status.get("reason", ""))
    if bool(req.get("strict")) and not bool(status.get("operational")):
        reason = str(status.get("reason") or "no_face_detector_backend")
        logger.emit(
            "error",
            "main.run",
            "face_detector_unavailable",
            {"reason": reason, "requested_backend": status.get("requested_backend")},
        )
        raise RuntimeError(f"Runtime requirements check failed:\n- no operational face detector backend: {reason}")


def _apply_runtime_thread_caps(config: Dict[str, Any], logger: LogEmitter) -> None:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    perf_profile = str(runtime_cfg.get("perf_profile", "default") or "default").strip().lower()
    if perf_profile != "realtime_pi_max":
        return
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    try:
        import cv2  # type: ignore

        cv2.setNumThreads(1)
        logger.emit(
            "info",
            "main.run",
            "thread_caps_applied",
            {
                "perf_profile": perf_profile,
                "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
                "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
                "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
                "opencv_threads": 1,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "warning",
            "main.run",
            "thread_caps_partial",
            {
                "perf_profile": perf_profile,
                "error": str(exc),
                "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
                "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
                "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
            },
        )


def _apply_runtime_scheduling(config: Dict[str, Any], logger: LogEmitter) -> None:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    scheduling_cfg = runtime_cfg.get("scheduling", {})
    if scheduling_cfg is None:
        scheduling_cfg = {}
    if not isinstance(scheduling_cfg, dict):
        logger.emit("warning", "main.run", "niceness_unapplied", {"reason": "invalid_config"})
        return
    niceness = int(scheduling_cfg.get("niceness", 0) or 0)
    if niceness == 0:
        return
    try:
        os.nice(niceness)
    except Exception as exc:  # noqa: BLE001
        logger.emit("warning", "main.run", "niceness_unapplied", {"niceness": niceness, "error": str(exc)})
        return
    logger.emit("info", "main.run", "niceness_applied", {"niceness": niceness})


def _join_threads(threads: List[threading.Thread], logger: LogEmitter, timeout_s: float) -> List[str]:
    alive: List[str] = []
    joined = 0
    current = threading.current_thread()
    for thread in threads:
        if thread is None or thread is current:
            continue
        try:
            thread.join(timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            alive.append(f"{getattr(thread, 'name', '<unknown>')} (join_error={exc})")
            continue
        joined += 1
        if thread.is_alive():
            alive.append(getattr(thread, "name", "<unknown>"))
    if alive:
        logger.emit(
            "warning",
            "main.run",
            "shutdown_incomplete",
            {"alive_threads": alive, "join_timeout_s": timeout_s, "joined_threads": joined},
        )
    else:
        logger.emit(
            "info",
            "main.run",
            "shutdown_complete",
            {"join_timeout_s": timeout_s, "joined_threads": joined},
        )
    return alive


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


def _runtime_requirements(config: Dict[str, Any]) -> Dict[str, Any]:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    req_cfg = runtime_cfg.get("requirements", {})
    if not isinstance(req_cfg, dict):
        req_cfg = {}
    raw_scope = req_cfg.get("camera_scope", "any")
    try:
        camera_scope = normalize_camera_scope(raw_scope)
    except Exception:
        camera_scope = "any"
    require_led_hid = bool(req_cfg.get("require_led_hid", False))
    runtime_check_raw = req_cfg.get("require_led_hid_runtime_check")
    require_led_hid_runtime_check = require_led_hid if runtime_check_raw is None else bool(runtime_check_raw)
    return {
        "strict": bool(req_cfg.get("strict", False)),
        "min_cameras": int(req_cfg.get("min_cameras", 0) or 0),
        "min_audio_channels": int(req_cfg.get("min_audio_channels", 0) or 0),
        "camera_scope": camera_scope,
        "require_led_hid": require_led_hid,
        "require_led_hid_runtime_check": require_led_hid_runtime_check,
    }


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
    if "minidsp" not in str(name).lower():
        return ""
    if channels >= required:
        return ""
    if required < 8:
        return ""
    return " hint: miniDSP UMA-8 appears in 2ch DSP mode; switch to RAW firmware for 8ch."


def _validate_runtime_requirements(config: Dict[str, Any], logger: LogEmitter) -> None:
    req = _runtime_requirements(config)
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


def _validate_led_hid_runtime(config: Dict[str, Any], logger: LogEmitter, req: Dict[str, Any]) -> None:
    require_led_hid = bool(req.get("require_led_hid", False))
    require_runtime_check = bool(req.get("require_led_hid_runtime_check", require_led_hid))
    if not (require_led_hid and require_runtime_check):
        return

    uma8_cfg = config.get("uma8_leds", {})
    if not isinstance(uma8_cfg, dict):
        uma8_cfg = {}
    vendor_id = int(uma8_cfg.get("vendor_id", 0x2752) or 0x2752)
    product_id = int(uma8_cfg.get("product_id", 0x001C) or 0x001C)

    try:
        import hid  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "error",
            "main.run",
            "led_hid_runtime_check_failed",
            {
                "reason": "hid_import_failed",
                "error": str(exc),
                "vendor_id": vendor_id,
                "product_id": product_id,
            },
        )
        raise RuntimeError(f"Runtime requirements check failed:\n- HID import failed: {exc}")

    try:
        devices = hid.enumerate(vendor_id, product_id)
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "error",
            "main.run",
            "led_hid_runtime_check_failed",
            {
                "reason": "hid_enumerate_failed",
                "error": str(exc),
                "vendor_id": vendor_id,
                "product_id": product_id,
            },
        )
        raise RuntimeError(f"Runtime requirements check failed:\n- HID enumerate failed: {exc}")

    device_count = len(devices)
    if device_count <= 0:
        logger.emit(
            "error",
            "main.run",
            "led_hid_runtime_check_failed",
            {
                "reason": "hid_no_device",
                "vendor_id": vendor_id,
                "product_id": product_id,
                "device_count": device_count,
            },
        )
        raise RuntimeError(
            "Runtime requirements check failed:\n"
            f"- HID device not found for vid=0x{vendor_id:04x} pid=0x{product_id:04x}"
        )

    logger.emit(
        "info",
        "main.run",
        "led_hid_runtime_check_passed",
        {
            "vendor_id": vendor_id,
            "product_id": product_id,
            "device_count": device_count,
        },
    )


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


def main() -> None:
    parser = argparse.ArgumentParser(description="FocusField vision-first runner")
    parser.add_argument("--config", default="configs/mvp_1cam_4mic.yaml", help="Path to YAML config")
    parser.add_argument("--mode", default="vision", help="Run mode (vision)")
    args = parser.parse_args()

    config = load_config(args.config)
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
    bus = Bus(
        max_queue_depth=int(bus_cfg.get("max_queue_depth", 8)),
        topic_queue_depths=parsed_topic_depths,
    )
    logger = LogEmitter(bus, min_level=config.get("logging", {}).get("level", "info"), run_id=str(config.get("runtime", {}).get("run_id", "")))
    bus_camera_topics = []
    cameras_cfg = config.get("video", {}).get("cameras", [])
    if isinstance(cameras_cfg, list):
        for idx, cam_cfg in enumerate(cameras_cfg):
            camera_id = f"cam{idx}"
            if isinstance(cam_cfg, dict):
                camera_id = str(cam_cfg.get("id", camera_id))
            bus_camera_topics.append(f"vision.frames.{camera_id}")
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
    _resolve_face_detector_backend(config, logger)
    _apply_runtime_thread_caps(config, logger)
    _apply_runtime_scheduling(config, logger)

    crash_event, crash_info = _install_crash_handlers(bus, run_dir, config, logger, stop_event)

    threads: List[threading.Thread] = []
    if args.mode not in {"vision"}:
        logger.emit("error", "main.run", "invalid_mode", {"mode": args.mode})
        raise SystemExit(f"Unsupported mode: {args.mode}")
    req = _runtime_requirements(config)
    try:
        _validate_runtime_requirements(config, logger)
        _validate_led_hid_runtime(config, logger, req)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    try:
        _validate_face_detector_requirements(config, logger, req)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    runtime_cfg = config.setdefault("runtime", {})
    if isinstance(runtime_cfg, dict):
        runtime_cfg["requirements_passed"] = True

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
    face_tracking_threads = start_face_tracking(bus, config, logger, stop_event)
    if isinstance(face_tracking_threads, list):
        threads.extend(face_tracking_threads)
    elif face_tracking_threads is not None:
        threads.append(face_tracking_threads)
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    threads.append(start_av_association(bus, config, logger, stop_event))
    threads.append(start_lock_state_machine(bus, config, logger, stop_event))
    threads.append(start_uma8_led_service(bus, config, logger, stop_event))

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

    join_timeout_s = float(config.get("runtime", {}).get("shutdown", {}).get("thread_join_timeout_s", 1.5) or 1.5)
    join_timeout_s = max(0.1, min(5.0, join_timeout_s))
    alive_threads = _join_threads(threads, logger, join_timeout_s)

    if crash_event.is_set() and bool(config.get("runtime", {}).get("fail_fast", True)):
        crash_info = dict(crash_info)
        crash_info["alive_threads"] = alive_threads
        logger.emit("error", "main.run", "crashed", crash_info)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
