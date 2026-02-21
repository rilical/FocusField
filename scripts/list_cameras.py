#!/usr/bin/env python3
"""List stable camera paths and validate capture.

This script helps with "just works" bring-up on Raspberry Pi.

It prints `/dev/v4l/by-id` mappings (stable across reboots) and attempts
to open each discovered camera with OpenCV, forcing MJPEG when possible.

Run:
  python3 scripts/list_cameras.py
"""

from __future__ import annotations

import glob
import os

import cv2


def _try_open(path: str) -> tuple[bool, cv2.VideoCapture]:
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if cap.isOpened():
        return True, cap
    cap.release()
    cap = cv2.VideoCapture(path)
    return cap.isOpened(), cap

def main() -> None:
    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    print("=== /dev/v4l/by-id ===")
    if not by_id:
        print("(none)")
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
        ok, cap = _try_open(p)
        if not cap.isOpened():
            print(f"OPEN FAIL: {p}")
            continue
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"READ FAIL: {p}")
        else:
            h, w = frame.shape[:2]
            print(f"OK: {p} frame={w}x{h}")
        cap.release()


if __name__ == "__main__":
    main()
