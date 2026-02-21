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
from pathlib import Path
from typing import Any, Dict, List

import cv2

from focusfield.core.clock import now_ns


def start_cameras(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> List[threading.Thread]:
    cameras = config.get("video", {}).get("cameras", [])
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
            args=(bus, logger, stop_event, camera_id, device_path, device_index, width, height, fps, topic),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _camera_loop(
    bus: Any,
    logger: Any,
    stop_event: threading.Event,
    camera_id: str,
    device_path: object,
    device_index: int,
    width: int,
    height: int,
    fps: int,
    topic: str,
) -> None:
    cap = _open_camera(device_path, device_index)
    if not cap.isOpened():
        logger.emit("error", "vision.cameras", "camera_missing", {"camera_id": camera_id})
        return
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:  # noqa: BLE001
        pass
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    seq = 0
    while not stop_event.is_set():
        ok, frame = cap.read()
        if not ok:
            logger.emit("warning", "vision.cameras", "frame_drop", {"camera_id": camera_id})
            time.sleep(0.05)
            continue
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
    cap.release()


def _open_camera(device_path: object, device_index: int) -> cv2.VideoCapture:
    # NOTE: some OpenCV builds can fail opening by-id paths with CAP_V4L2.
    # Resolve symlinks first and then try the integer index fallback.
    source = str(device_path) if device_path else int(device_index)
    resolved = source
    try:
        if isinstance(device_path, str):
            resolved_path = str(Path(device_path).resolve())
            if resolved_path:
                resolved = resolved_path
    except Exception:  # noqa: BLE001
        pass

    candidates = []
    for value in (source, resolved):
        if value not in candidates:
            candidates.append(value)

    # If we still have a by-id-style path, also try the numeric node.
    if isinstance(device_path, str) and "by-id" in device_path:
        if str(source).endswith(f"video-index{device_index}"):
            candidates.append(int(device_index))
        # Fallback: if realpath is /dev/videoX, use that path explicitly.
        if isinstance(resolved, str) and resolved.startswith("/dev/video"):
            candidates.append(resolved)

    for candidate in candidates:
        cap = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
        if cap.isOpened():
            return cap
        cap.release()

    # Fallback for environments where CAP_V4L2 is unavailable/unstable.
    for candidate in candidates:
        cap = cv2.VideoCapture(candidate, cv2.CAP_ANY)
        if cap.isOpened():
            return cap
        cap.release()

    return cv2.VideoCapture()
