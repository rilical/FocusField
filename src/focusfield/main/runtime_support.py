from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
from typing import Any, Dict, List, Optional

from focusfield.core.bus import Bus
from focusfield.platform.hardware_probe import normalize_camera_scope


def build_bus(config: Dict[str, Any]) -> Bus:
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
    return Bus(
        max_queue_depth=int(bus_cfg.get("max_queue_depth", 8)),
        topic_queue_depths=parsed_topic_depths,
    )


def camera_ids(config: Dict[str, Any]) -> List[str]:
    cameras_cfg = config.get("video", {}).get("cameras", [])
    if not isinstance(cameras_cfg, list):
        cameras_cfg = []
    ids: List[str] = []
    for idx, cam_cfg in enumerate(cameras_cfg):
        camera_id = f"cam{idx}"
        if isinstance(cam_cfg, dict):
            camera_id = str(cam_cfg.get("id", camera_id))
        ids.append(camera_id)
    return ids


def camera_topics(config: Dict[str, Any]) -> List[str]:
    return [f"vision.frames.{camera_id}" for camera_id in camera_ids(config)]


def runtime_requirements(config: Dict[str, Any]) -> Dict[str, Any]:
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
    return {
        "strict": bool(req_cfg.get("strict", False)),
        "min_cameras": int(req_cfg.get("min_cameras", 0) or 0),
        "min_audio_channels": int(req_cfg.get("min_audio_channels", 0) or 0),
        "camera_scope": camera_scope,
    }


def runtime_process_mode(config: Dict[str, Any]) -> str:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    return str(runtime_cfg.get("process_mode", "threaded") or "threaded").strip().lower()


def start_beamformed_passthrough(bus: Bus, logger: Any, stop_event: threading.Event, module_name: str) -> threading.Thread:
    """Republish beamformed audio to the final topic when denoise is disabled."""

    q = bus.subscribe("audio.enhanced.beamformed")

    def _run() -> None:
        while not stop_event.is_set():
            try:
                msg = q.get(timeout=0.1)
            except queue.Empty:
                continue
            bus.publish("audio.enhanced.final", msg)

    thread = threading.Thread(target=_run, name=f"{module_name}-passthrough", daemon=True)
    thread.start()
    logger.emit("info", module_name, "denoise_disabled_passthrough", {})
    return thread


def apply_runtime_thread_caps(config: Dict[str, Any], logger: Any, role: str = "main") -> None:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    perf_profile = str(runtime_cfg.get("perf_profile", "default") or "default").strip().lower()
    realtime_cfg = _resolve_realtime_cfg(config, role)
    if perf_profile != "realtime_pi_max" and not bool(realtime_cfg.get("enabled", False)):
        return
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    payload = {
        "role": role,
        "perf_profile": perf_profile,
        "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
        "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
    }
    try:
        import cv2  # type: ignore

        cv2.setNumThreads(1)
        payload["opencv_threads"] = 1
        logger.emit("info", "main.runtime", "thread_caps_applied", payload)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = str(exc)
        logger.emit("warning", "main.runtime", "thread_caps_partial", payload)


def apply_runtime_os_tuning(config: Dict[str, Any], logger: Any, role: str = "main") -> None:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    perf_profile = str(runtime_cfg.get("perf_profile", "default") or "default").strip().lower()
    realtime_cfg = _resolve_realtime_cfg(config, role)
    enabled = bool(realtime_cfg.get("enabled", perf_profile == "realtime_pi_max"))
    if not enabled:
        return

    allow_best_effort = bool(realtime_cfg.get("allow_best_effort", True))
    payload: Dict[str, Any] = {
        "role": role,
        "enabled": enabled,
        "allow_best_effort": allow_best_effort,
        "applied": [],
        "failed": [],
    }

    cpu_affinity = realtime_cfg.get("cpu_affinity", [])
    if isinstance(cpu_affinity, (list, tuple)) and cpu_affinity and hasattr(os, "sched_setaffinity"):
        try:
            cpus = sorted({int(cpu) for cpu in cpu_affinity})
            os.sched_setaffinity(0, cpus)
            payload["applied"].append({"setting": "cpu_affinity", "cpus": cpus})
        except Exception as exc:  # noqa: BLE001
            payload["failed"].append({"setting": "cpu_affinity", "error": str(exc)})

    scheduler_name = str(realtime_cfg.get("scheduler", "other") or "other").strip().lower()
    scheduler_map = {
        "other": getattr(os, "SCHED_OTHER", None),
        "fifo": getattr(os, "SCHED_FIFO", None),
        "rr": getattr(os, "SCHED_RR", None),
    }
    scheduler = scheduler_map.get(scheduler_name)
    if scheduler is not None and hasattr(os, "sched_setscheduler"):
        try:
            priority = int(realtime_cfg.get("priority", 0 if scheduler_name == "other" else 10))
            os.sched_setscheduler(0, scheduler, os.sched_param(priority))
            payload["applied"].append({"setting": "scheduler", "policy": scheduler_name, "priority": priority})
        except Exception as exc:  # noqa: BLE001
            payload["failed"].append({"setting": "scheduler", "policy": scheduler_name, "error": str(exc)})

    if bool(realtime_cfg.get("mlockall", False)):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            mcl_current = 1
            mcl_future = 2
            if libc.mlockall(mcl_current | mcl_future) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
            payload["applied"].append({"setting": "mlockall"})
        except Exception as exc:  # noqa: BLE001
            payload["failed"].append({"setting": "mlockall", "error": str(exc)})

    nice_value = realtime_cfg.get("nice")
    if nice_value is not None:
        try:
            os.nice(int(nice_value))
            payload["applied"].append({"setting": "nice", "value": int(nice_value)})
        except Exception as exc:  # noqa: BLE001
            payload["failed"].append({"setting": "nice", "value": nice_value, "error": str(exc)})

    if payload["failed"]:
        level = "warning" if allow_best_effort else "error"
        logger.emit(level, "main.runtime", "os_tuning_partial", payload)
        if not allow_best_effort:
            raise RuntimeError(f"failed to apply runtime tuning: {json.dumps(payload['failed'])}")
    else:
        logger.emit("info", "main.runtime", "os_tuning_applied", payload)


def _resolve_realtime_cfg(config: Dict[str, Any], role: str) -> Dict[str, Any]:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    base = runtime_cfg.get("realtime", {})
    if not isinstance(base, dict):
        base = {}
    merged = dict(base)
    mp_cfg = runtime_cfg.get("multiprocess", {})
    if not isinstance(mp_cfg, dict):
        mp_cfg = {}
    workers = mp_cfg.get("workers", {})
    if not isinstance(workers, dict):
        workers = {}
    override = workers.get(role, {})
    if isinstance(override, dict):
        for key, value in override.items():
            merged[key] = value
    return merged
