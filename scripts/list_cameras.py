#!/usr/bin/env python3
"""List stable camera paths and validate capture.

This script helps with "just works" bring-up on Raspberry Pi.

It prints `/dev/v4l/by-id` mappings (stable across reboots) and attempts
to open each discovered camera with OpenCV, resolving symlinks and trying
fallback nodes before declaring failure.

Run:
  python3 scripts/list_cameras.py
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

import cv2

_V4L2_CAPTURE_BITS = (0x00000001, 0x00001000, 0x0000000200, 0x0000080000)


def _video_index_for_source(path: str) -> int | None:
    match = re.search(r"/dev/video(\d+)$", path)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _is_capture_node(path: str) -> bool | None:
    index = _video_index_for_source(path)
    if index is None:
        return None

    capabilities_path = Path(f"/sys/class/video4linux/video{index}/capabilities")
    if not capabilities_path.exists():
        return None

    try:
        raw = capabilities_path.read_text(encoding="utf-8", errors="ignore").strip()
        caps = int(raw, 0)
    except Exception:
        return None

    return any(caps & bit for bit in _V4L2_CAPTURE_BITS)


def _candidate_sources(path: str) -> list[str]:
    """Return candidate open sources for a by-id camera path."""
    sources: list[str] = []
    path_is_video = path.startswith("/dev/video")
    path_is_by_id = path.startswith("/dev/v4l/by-id/")

    if path_is_video and _is_capture_node(path) is False:
        return []

    if path_is_video:
        sources.append(path)

    resolved = None
    try:
        resolved = os.path.realpath(path)
    except Exception:  # noqa: BLE001
        resolved = None

    if resolved and resolved != path:
        if resolved.startswith("/dev/video"):
            if _is_capture_node(resolved) is False:
                return []
            if resolved not in sources:
                sources.append(resolved)
            m = re.search(r"/dev/video(\d+)$", resolved)
            if m is not None:
                video_source = f"/dev/video{m.group(1)}"
                if video_source not in sources:
                    sources.append(video_source)
            return list(dict.fromkeys(sources))

        if resolved not in sources:
            sources.append(resolved)

    # For stable by-id links where realpath fails to map to /dev/video,
    # keep the original path as fallback.
    if not path_is_by_id or resolved is None:
        if path not in sources:
            sources.append(path)

    if not path_is_video and path not in sources:
        sources.append(path)

    if path_is_video and sources == [path] and _is_capture_node(path) is False:
        return []

    return list(dict.fromkeys(sources))


def _source_to_open_target(source: str) -> str | int:
    match = re.search(r"/dev/video(\d+)$", source)
    if match is None:
        return source
    try:
        return int(match.group(1))
    except Exception:
        return source


def _try_open(path: str) -> tuple[bool, cv2.VideoCapture, str]:
    candidates = _candidate_sources(path)
    if not candidates:
        return False, cv2.VideoCapture(), "skip"
    for source in candidates:
        source_obj = _source_to_open_target(source)
        cap = cv2.VideoCapture(source_obj, cv2.CAP_V4L2)
        if cap.isOpened():
            return True, cap, "CAP_V4L2"
        cap.release()

    for source in candidates:
        source_obj = _source_to_open_target(source)
        cap = cv2.VideoCapture(source_obj, cv2.CAP_ANY)
        if cap.isOpened():
            return True, cap, "CAP_ANY"
        cap.release()
    return False, cv2.VideoCapture(), "none"


def main() -> None:
    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    print("=== /dev/v4l/by-id ===")
    if not by_id:
        print("(none)")
        return

    for p in by_id:
        try:
            target = os.path.realpath(p)
        except Exception:
            target = "?"
        print(f"{p} -> {target}")

    if not hasattr(cv2, "VideoCapture"):
        print("OpenCV not available: missing VideoCapture")
        return

    print("\n=== OpenCV open test ===")
    for p in by_id:
        sources = _candidate_sources(p)
        ok, cap, backend = _try_open(p)
        if not ok or not cap.isOpened():
            print(f"OPEN FAIL: {p} backend={backend} tried={sources}")
            continue
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:  # noqa: BLE001
            pass
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"READ FAIL: {p} backend={backend} tried={sources}")
        else:
            h, w = frame.shape[:2]
            print(f"OK: {p} frame={w}x{h} backend={backend} tried={sources}")
        cap.release()


if __name__ == "__main__":
    main()
