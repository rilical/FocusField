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

    try:
        import cv2
    except Exception as exc:  # noqa: BLE001
        print(f"OpenCV not available: {exc}")
        return

    print("\n=== OpenCV open test ===")
    for p in by_id:
        cap = cv2.VideoCapture(p)
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

