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

import threading
import time
from typing import Any, Dict, List

import cv2

from focusfield.core.clock import now_ns
from focusfield.platform.hardware_probe import candidate_sources, is_capture_node, source_to_open_target

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
    threads: List[threading.Thread] = []
    for index, cam_cfg in enumerate(cameras):
        camera_id = cam_cfg.get("id", f"cam{index}")
        device_path = cam_cfg.get("device_path")
        device_index = cam_cfg.get("device_index", index)
        width = cam_cfg.get("width", 640)
        height = cam_cfg.get("height", 480)
        fps = cam_cfg.get("fps", 30)
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
                topic,
                strict_capture,
                camera_scope,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _configure_capture(cap: cv2.VideoCapture, width: int, height: int, fps: float) -> None:
    """Apply capture settings to an opened VideoCapture."""
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:  # noqa: BLE001
        pass
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)


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
            _configure_capture(cap, width, height, fps)
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
    topic: str,
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
        if fail_fast and strict_capture:
            stop_event.set()
            return
        # Enter reconnect loop instead of giving up
        cap.release()
        cap_new = _attempt_reconnect(
            bus, logger, stop_event, camera_id,
            device_path, device_index, width, height, float(max(1.0, float(fps))),
            strict_capture, camera_scope,
        )
        if cap_new is None:
            return
        cap = cap_new
    else:
        _publish_camera_status(bus, camera_id, True)

    fps = max(1.0, float(fps))
    frame_period_s = 1.0 / fps
    _configure_capture(cap, width, height, fps)
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
                    device_path, device_index, width, height, fps,
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
