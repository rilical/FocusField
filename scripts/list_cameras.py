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

import cv2

from focusfield.platform.hardware_probe import (
    collect_camera_sources,
    source_to_open_target,
    try_open_camera_any_backend,
)


def _unique_tried_sources(tried: list[tuple[object, str]]) -> list[object]:
    out: list[object] = []
    for source, _backend in tried:
        if source not in out:
            out.append(source)
    return out


def main() -> None:
    by_path = sorted(glob.glob("/dev/v4l/by-path/*"))
    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    sources = collect_camera_sources("auto")

    print("=== /dev/v4l/by-path ===")
    if by_path:
        for p in by_path:
            try:
                target = os.path.realpath(p)
            except Exception:
                target = "?"
            print(f"{p} -> {target}")
    else:
        print("(none)")

    print("=== /dev/v4l/by-id ===")
    if not by_id:
        print("(none)")
    else:
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
    for p in sources:
        ok, tried, opened = try_open_camera_any_backend(p, strict_capture=False)
        backend = opened[1] if opened is not None else "none"
        candidates = _unique_tried_sources(tried)
        if not ok or opened is None:
            print(f"OPEN FAIL: {p} backend={backend} tried={candidates}")
            continue
        cap = cv2.VideoCapture(source_to_open_target(opened[0]), cv2.CAP_V4L2 if backend == "CAP_V4L2" else cv2.CAP_ANY)
        if not cap.isOpened():
            cap.release()
            print(f"OPEN FAIL: {p} backend={backend} tried={candidates}")
            continue
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:  # noqa: BLE001
            pass
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"READ FAIL: {p} backend={backend} tried={candidates}")
        else:
            h, w = frame.shape[:2]
            print(f"OK: {p} frame={w}x{h} backend={backend} tried={candidates}")
        cap.release()


if __name__ == "__main__":
    main()
