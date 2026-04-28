"""
CONTRACT: inline (source: src/focusfield/vision/cameras.md)
ROLE: Multi-camera capture.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: vision.frames.cam0  Type: VideoFrame
  - Topic: vision.frames.cam1  Type: VideoFrame
  - Topic: vision.frames.cam2  Type: VideoFrame

CONFIG KEYS:
  - video.cameras[].device_index: camera index
  - video.cameras[].width: frame width
  - video.cameras[].height: frame height
  - video.cameras[].fps: frame rate
  - video.cameras[].hfov_deg: camera HFOV
  - video.cameras[].yaw_offset_deg: yaw offset

PERF / TIMING:
  - stable frame rate

FAILURE MODES:
  - camera missing -> mark degraded -> log camera_missing

LOG EVENTS:
  - module=vision.cameras, event=camera_missing, payload keys=camera_id

TESTS:
  - tests/usb_bandwidth_sanity.md must cover aggregate camera load

CONTRACT DETAILS (inline from src/focusfield/vision/cameras.md):
# Camera capture

- Support multi-camera capture with per-camera IDs.
- Timestamp frames and emit VideoFrame.
- Detect and log frame drops.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Mapping

import cv2
import numpy as np

from focusfield.core.clock import now_ns
from focusfield.platform.hardware_probe import (
    candidate_sources,
    collect_camera_sources,
    is_capture_node,
    source_to_open_target,
    video_index_for_source,
)

_RECONNECT_INTERVAL_S = 2.0
_MAX_CONSECUTIVE_FAILURES = 10


def _publish_camera_status(bus: Any, camera_id: str, connected: bool) -> None:
    """Publish camera connection status on the bus."""
    bus.publish(f"vision.camera_status.{camera_id}", {
        "camera_id": camera_id,
        "connected": connected,
        "t_ns": now_ns(),
    })


def start_cameras(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> List[threading.Thread]:
    cameras = config.get("video", {}).get("cameras", [])
    fail_fast = bool(config.get("runtime", {}).get("fail_fast", True))
    video_cfg = config.get("video", {})
    if isinstance(cameras, list) and isinstance(video_cfg, dict):
        cameras = _resolve_runtime_camera_sources(
            cameras,
            video_cfg,
            logger,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
    threads: List[threading.Thread] = []
    for index, cam_cfg in enumerate(cameras):
        camera_id = cam_cfg.get("id", f"cam{index}")
        device_path = cam_cfg.get("device_path")
        device_index = cam_cfg.get("device_index", index)
        width = cam_cfg.get("width", 640)
        height = cam_cfg.get("height", 480)
        fps = cam_cfg.get("fps", 30)
        fourcc = str(cam_cfg.get("fourcc", config.get("video", {}).get("fourcc", "MJPG")) or "MJPG").strip().upper()
        video_cfg = config.get("video", {})
        controls_cfg = {}
        frame_adjustment_cfg = {}
        if isinstance(video_cfg, dict):
            controls_cfg = _resolve_camera_controls(video_cfg, cam_cfg)
            frame_adjustment_cfg = _resolve_frame_adjustment(video_cfg, cam_cfg)
        topic = f"vision.frames.{camera_id}"
        thread = threading.Thread(
            target=_camera_loop,
            name=f"camera-{camera_id}",
            args=(
                bus,
                logger,
                stop_event,
                fail_fast,
                camera_id,
                device_path,
                device_index,
                width,
                height,
                fps,
                fourcc,
                topic,
                controls_cfg,
                frame_adjustment_cfg,
                strict_capture,
                camera_scope,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _resolve_runtime_camera_sources(
    cameras: list[Any],
    video_cfg: Mapping[str, Any],
    logger: Any,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> list[Any]:
    """Replace stale /dev/videoN camera bindings with current stable USB paths."""
    if not bool(video_cfg.get("auto_rebind_sources", True)):
        return cameras
    camera_dicts = [cam for cam in cameras if isinstance(cam, Mapping)]
    if not camera_dicts:
        return cameras

    needs_rebind = any(
        _camera_binding_stale(cam, idx, strict_capture=strict_capture, camera_scope=camera_scope)
        for idx, cam in enumerate(cameras)
        if isinstance(cam, Mapping)
    )
    if not needs_rebind:
        return cameras

    source_mode = str(video_cfg.get("camera_source", "by-path") or "by-path").strip().lower()
    if source_mode not in {"by-path", "by-id", "index", "auto"}:
        source_mode = "by-path"
    try:
        discovered = collect_camera_sources(source_mode, camera_scope=camera_scope)
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "warning",
            "vision.cameras",
            "camera_source_rebind_failed",
            {"reason": "probe_failed", "error": str(exc), "camera_scope": camera_scope},
        )
        return cameras

    if len(discovered) < len(camera_dicts):
        logger.emit(
            "warning",
            "vision.cameras",
            "camera_source_rebind_failed",
            {
                "reason": "insufficient_sources",
                "required": len(camera_dicts),
                "discovered": len(discovered),
                "camera_scope": camera_scope,
                "sources": discovered,
            },
        )
        return cameras

    rebound: list[Any] = []
    mapping: list[dict[str, Any]] = []
    source_iter = iter(discovered)
    for idx, cam in enumerate(cameras):
        if not isinstance(cam, Mapping):
            rebound.append(cam)
            continue
        source = next(source_iter)
        resolved = _realpath_or_self(source)
        resolved_index = video_index_for_source(resolved)
        if resolved_index is None:
            resolved_index = video_index_for_source(source)
        updated = dict(cam)
        old_path = updated.get("device_path")
        old_index = updated.get("device_index")
        updated["device_path"] = source
        if resolved_index is not None:
            updated["device_index"] = resolved_index
        rebound.append(updated)
        mapping.append(
            {
                "camera_id": str(updated.get("id", f"cam{idx}")),
                "old_device_path": old_path,
                "old_device_index": old_index,
                "device_path": source,
                "resolved_path": resolved,
                "device_index": updated.get("device_index"),
            }
        )

    logger.emit(
        "info",
        "vision.cameras",
        "camera_sources_rebound",
        {
            "camera_scope": camera_scope,
            "camera_source": source_mode,
            "mapping": mapping,
        },
    )
    return rebound


def _camera_binding_stale(
    cam_cfg: Mapping[str, Any],
    idx: int,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> bool:
    device_path = cam_cfg.get("device_path")
    if isinstance(device_path, str) and device_path.strip():
        candidates = candidate_sources(
            device_path.strip(),
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
        return not any(_source_present(candidate, strict_capture=strict_capture) for candidate in candidates)
    # Blank paths plus integer indices are not stable across UVC re-enumeration.
    return True


def _source_present(source: object, strict_capture: bool = False) -> bool:
    path: str | None = None
    if isinstance(source, int):
        path = f"/dev/video{source}"
    elif isinstance(source, str):
        raw = source.strip()
        if not raw:
            return False
        path = _realpath_or_self(raw)
    if not path or not path.startswith("/dev/video"):
        return False
    if not os.path.exists(path):
        return False
    if strict_capture and is_capture_node(path) is False:
        return False
    return True


def _realpath_or_self(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return path


def _configure_capture(
    cap: cv2.VideoCapture,
    width: int,
    height: int,
    fps: float,
    fourcc: str,
    logger: Any,
    camera_id: str,
    control_device: str | None = None,
    controls_cfg: Mapping[str, Any] | None = None,
) -> None:
    """Apply capture settings to an opened VideoCapture."""
    try:
        normalized_fourcc = str(fourcc or "MJPG").strip().upper()
        if len(normalized_fourcc) == 4:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*normalized_fourcc))
    except Exception:  # noqa: BLE001
        pass
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    _apply_camera_controls(logger, camera_id, control_device, controls_cfg)


def _resolve_camera_controls(video_cfg: Mapping[str, Any], cam_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    controls_cfg = video_cfg.get("camera_controls", {})
    if not isinstance(controls_cfg, Mapping):
        controls_cfg = {}
    defaults = controls_cfg.get("defaults", {})
    if not isinstance(defaults, Mapping):
        defaults = {}
    overrides = cam_cfg.get("controls", {})
    if not isinstance(overrides, Mapping):
        overrides = {}
    return {
        "enabled": bool(controls_cfg.get("enabled", False)),
        "settle_ms": int(controls_cfg.get("settle_ms", 150) or 150),
        "defaults": dict(defaults),
        "overrides": dict(overrides),
    }


def _resolve_frame_adjustment(video_cfg: Mapping[str, Any], cam_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    adjust_cfg = video_cfg.get("frame_adjustment", {})
    if not isinstance(adjust_cfg, Mapping):
        adjust_cfg = {}
    defaults = adjust_cfg.get("defaults", {})
    if not isinstance(defaults, Mapping):
        defaults = {}
    overrides = cam_cfg.get("frame_adjustment", {})
    if not isinstance(overrides, Mapping):
        overrides = {}
    merged = dict(defaults)
    merged.update(overrides)
    return {
        "enabled": bool(adjust_cfg.get("enabled", False)),
        "alpha": float(merged.get("alpha", adjust_cfg.get("alpha", 1.0)) or 1.0),
        "beta": float(merged.get("beta", adjust_cfg.get("beta", 0.0)) or 0.0),
        "clahe": bool(merged.get("clahe", adjust_cfg.get("clahe", False))),
        "clip_limit": float(merged.get("clip_limit", adjust_cfg.get("clip_limit", 2.0)) or 2.0),
        "brightness_guard": bool(merged.get("brightness_guard", adjust_cfg.get("brightness_guard", False))),
        "target_mean": float(merged.get("target_mean", adjust_cfg.get("target_mean", 135.0)) or 135.0),
        "max_gain": float(merged.get("max_gain", adjust_cfg.get("max_gain", 1.5)) or 1.5),
    }


def _adjust_frame_for_detection(frame, adjustment_cfg: Mapping[str, Any] | None):
    if not isinstance(adjustment_cfg, Mapping) or not bool(adjustment_cfg.get("enabled", False)):
        return frame
    out = np.asarray(frame)
    alpha = float(adjustment_cfg.get("alpha", 1.0) or 1.0)
    beta = float(adjustment_cfg.get("beta", 0.0) or 0.0)
    if bool(adjustment_cfg.get("brightness_guard", False)):
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        mean = float(np.mean(gray))
        target = float(adjustment_cfg.get("target_mean", 135.0) or 135.0)
        max_gain = max(0.1, float(adjustment_cfg.get("max_gain", 1.5) or 1.5))
        if mean > 1.0:
            alpha *= float(np.clip(target / mean, 0.15, max_gain))
    adjusted = cv2.convertScaleAbs(out, alpha=alpha, beta=beta)
    if not bool(adjustment_cfg.get("clahe", False)):
        return adjusted
    lab = cv2.cvtColor(adjusted, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    clip_limit = max(0.1, float(adjustment_cfg.get("clip_limit", 2.0) or 2.0))
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    l_chan = clahe.apply(l_chan)
    return cv2.cvtColor(cv2.merge((l_chan, a_chan, b_chan)), cv2.COLOR_LAB2BGR)


def _camera_control_device(device_path: object, device_index: int) -> str | None:
    if isinstance(device_path, str) and device_path.strip():
        raw = device_path.strip()
        try:
            resolved = os.path.realpath(raw)
        except Exception:
            resolved = raw
        return resolved or raw
    try:
        idx = int(device_index)
    except (TypeError, ValueError):
        return None
    if idx < 0:
        return None
    return f"/dev/video{idx}"


def _serialize_v4l2_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return str(int(value))
    return None


def _apply_camera_controls(
    logger: Any,
    camera_id: str,
    control_device: str | None,
    controls_cfg: Mapping[str, Any] | None,
) -> None:
    if not isinstance(controls_cfg, Mapping) or not bool(controls_cfg.get("enabled", False)):
        return
    if not control_device:
        return
    v4l2_ctl = shutil.which("v4l2-ctl")
    if not v4l2_ctl:
        logger.emit(
            "warning",
            "vision.cameras",
            "camera_controls_unavailable",
            {"camera_id": camera_id, "device": control_device, "reason": "v4l2-ctl missing"},
        )
        return

    merged: Dict[str, Any] = {}
    defaults = controls_cfg.get("defaults", {})
    if isinstance(defaults, Mapping):
        merged.update(defaults)
    overrides = controls_cfg.get("overrides", {})
    if isinstance(overrides, Mapping):
        merged.update(overrides)
    if not merged:
        return

    assignments: list[str] = []
    skipped: list[str] = []
    for key, value in merged.items():
        serialized = _serialize_v4l2_value(value)
        if serialized is None:
            skipped.append(str(key))
            continue
        assignments.append(f"{key}={serialized}")
    if not assignments:
        return

    try:
        subprocess.run(
            [v4l2_ctl, "-d", control_device, "--set-ctrl", ",".join(assignments)],
            check=True,
            capture_output=True,
            text=True,
        )
        settle_ms = max(0, int(controls_cfg.get("settle_ms", 150) or 150))
        if settle_ms:
            time.sleep(settle_ms / 1000.0)
        logger.emit(
            "info",
            "vision.cameras",
            "camera_controls_applied",
            {
                "camera_id": camera_id,
                "device": control_device,
                "controls": assignments,
                "skipped": skipped,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "warning",
            "vision.cameras",
            "camera_controls_failed",
            {
                "camera_id": camera_id,
                "device": control_device,
                "controls": assignments,
                "error": str(exc),
                "skipped": skipped,
            },
        )


def _attempt_reconnect(
    bus: Any,
    logger: Any,
    stop_event: threading.Event,
    camera_id: str,
    device_path: object,
    device_index: int,
    width: int,
    height: int,
    fps: float,
    fourcc: str,
    controls_cfg: Mapping[str, Any] | None,
    strict_capture: bool,
    camera_scope: str,
) -> cv2.VideoCapture | None:
    """Try to reopen a camera, retrying every 2 seconds until success or stop."""
    _publish_camera_status(bus, camera_id, False)
    logger.emit("warning", "vision.cameras", "camera_disconnected", {"camera_id": camera_id})

    while not stop_event.is_set():
        time.sleep(_RECONNECT_INTERVAL_S)
        if stop_event.is_set():
            break
        cap = _open_camera(
            device_path,
            device_index,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
        if cap.isOpened():
            _configure_capture(
                cap,
                width,
                height,
                fps,
                fourcc,
                logger,
                camera_id,
                _camera_control_device(device_path, device_index),
                controls_cfg,
            )
            _publish_camera_status(bus, camera_id, True)
            logger.emit("info", "vision.cameras", "camera_reconnected", {"camera_id": camera_id})
            return cap
        cap.release()
    return None


def _camera_loop(
    bus: Any,
    logger: Any,
    stop_event: threading.Event,
    fail_fast: bool,
    camera_id: str,
    device_path: object,
    device_index: int,
    width: int,
    height: int,
    fps: int,
    fourcc: str,
    topic: str,
    controls_cfg: Mapping[str, Any] | None = None,
    frame_adjustment_cfg: Mapping[str, Any] | None = None,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> None:
    cap = _open_camera(
        device_path,
        device_index,
        strict_capture=strict_capture,
        camera_scope=camera_scope,
    )
    if not cap.isOpened():
        logger.emit("error", "vision.cameras", "camera_missing", {"camera_id": camera_id})
        _publish_camera_status(bus, camera_id, False)
        if fail_fast:
            stop_event.set()
            return
        # Enter reconnect loop instead of giving up
        cap.release()
        cap_new = _attempt_reconnect(
            bus, logger, stop_event, camera_id,
            device_path, device_index, width, height, float(max(1.0, float(fps))), fourcc,
            controls_cfg,
            strict_capture, camera_scope,
        )
        if cap_new is None:
            return
        cap = cap_new
    else:
        _publish_camera_status(bus, camera_id, True)

    fps = max(1.0, float(fps))
    frame_period_s = 1.0 / fps
    _configure_capture(
        cap,
        width,
        height,
        fps,
        fourcc,
        logger,
        camera_id,
        _camera_control_device(device_path, device_index),
        controls_cfg,
    )
    seq = 0
    consecutive_failures = 0
    next_deadline_s = time.perf_counter()
    while not stop_event.is_set():
        ok, frame = cap.read()
        if not ok:
            consecutive_failures += 1
            logger.emit("warning", "vision.cameras", "frame_drop", {"camera_id": camera_id})
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                # Camera appears disconnected; release and attempt reconnect
                cap.release()
                cap_new = _attempt_reconnect(
                    bus, logger, stop_event, camera_id,
                    device_path, device_index, width, height, fps, fourcc,
                    controls_cfg,
                    strict_capture, camera_scope,
                )
                if cap_new is None:
                    return
                cap = cap_new
                consecutive_failures = 0
                next_deadline_s = time.perf_counter()
            else:
                time.sleep(0.05)
            continue

        consecutive_failures = 0
        frame = _adjust_frame_for_detection(frame, frame_adjustment_cfg)
        t_ns = now_ns()
        seq += 1
        height_out, width_out = frame.shape[:2]
        msg = {
            "t_ns": t_ns,
            "seq": seq,
            "width": int(width_out),
            "height": int(height_out),
            "pixel_format": "bgr24",
            "data": frame,
            "camera_id": camera_id,
            "device_index": device_index,
            "device_path": str(device_path) if device_path else None,
        }
        bus.publish(topic, msg)
        next_deadline_s += frame_period_s
        sleep_s = next_deadline_s - time.perf_counter()
        if sleep_s > 0:
            time.sleep(sleep_s)
        else:
            next_deadline_s = time.perf_counter()
    cap.release()


def _camera_candidates(
    device_path: object,
    device_index: int,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> list[object]:
    candidates: list[object] = []
    if isinstance(device_path, str) and device_path.strip():
        candidates = candidate_sources(
            device_path.strip(),
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
        if not candidates:
            candidates.append(device_path.strip())

    try:
        index_candidate = int(device_index)
    except (TypeError, ValueError):
        index_candidate = None
    if index_candidate is None:
        return candidates

    if strict_capture:
        if is_capture_node(f"/dev/video{index_candidate}") is True:
            if index_candidate not in candidates:
                candidates.append(index_candidate)
    elif index_candidate not in candidates:
        candidates.append(index_candidate)

    deduped: list[object] = []
    for value in candidates:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _open_camera(
    device_path: object,
    device_index: int,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> cv2.VideoCapture:
    # NOTE: some OpenCV builds can fail opening by-id paths with CAP_V4L2.
    # Prefer resolved numeric /dev/videoN nodes over by-id paths.
    candidates = _camera_candidates(
        device_path,
        device_index,
        strict_capture=strict_capture,
        camera_scope=camera_scope,
    )

    backends = [cv2.CAP_V4L2]
    if not strict_capture:
        backends.append(cv2.CAP_ANY)

    for backend in backends:
        for candidate in candidates:
            source = source_to_open_target(candidate)
            cap = cv2.VideoCapture(source, backend)
            if cap.isOpened():
                return cap
            cap.release()

    return cv2.VideoCapture()
