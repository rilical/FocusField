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

import cv2


def _candidate_sources(path: str) -> list[str]:
    """Return candidate open sources for a by-id camera path."""
    sources: list[str] = [path]
    resolved: str | None = None
    try:
        resolved = os.path.realpath(path)
    except Exception:  # noqa: BLE001
        resolved = None
    else:
        if resolved and resolved != path:
            sources.append(resolved)
            m = re.search(r"/dev/video(\d+)$", resolved)
            if m is not None:
                sources.append(f"/dev/video{m.group(1)}")
    return list(dict.fromkeys(sources))


def _try_open(path: str) -> tuple[bool, cv2.VideoCapture]:
    for source in _candidate_sources(path):
        cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        if cap.isOpened():
            return True, cap
        cap.release()
        cap = cv2.VideoCapture(source, cv2.CAP_ANY)
        if cap.isOpened():
            return True, cap
        cap.release()
    return False, cv2.VideoCapture()


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
        ok, cap = _try_open(p)
        if not ok or not cap.isOpened():
            print(f"OPEN FAIL: {p} tried={sources}")
            continue
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:  # noqa: BLE001
            pass
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"READ FAIL: {p} tried={sources}")
        else:
            h, w = frame.shape[:2]
            print(f"OK: {p} frame={w}x{h} tried={sources}")
        cap.release()


if __name__ == "__main__":
    main()
