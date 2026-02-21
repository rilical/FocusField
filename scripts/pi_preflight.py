#!/usr/bin/env python3
"""Pi preflight check for FocusField hardware and runtime dependencies."""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

try:
    import cv2
except Exception as exc:  # pragma: no cover - runtime diagnostic
    cv2 = None
    cv2_error = exc
else:
    cv2_error = None

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print(f"Unable to import yaml: {exc}", flush=True)
    yaml = None

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    sd = None
    sounddevice_error = exc
else:
    sounddevice_error = None


def _safe_yaml_load(path: Path) -> dict:
    if yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"Could not load config {path}: {exc}")
        return {}


def _candidate_sources(path: str) -> list[str]:
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


def _try_open_camera(source: str) -> bool:
    if cv2 is None:
        return False
    for candidate in _candidate_sources(source):
        cap = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
        if cap.isOpened():
            cap.release()
            return True
        cap.release()
        cap = cv2.VideoCapture(candidate, cv2.CAP_ANY)
        if cap.isOpened():
            cap.release()
            return True
        cap.release()
    return False


def check_audio() -> int:
    print("=== Audio inputs ===")
    if sd is None:
        print(f"sounddevice import failed: {sounddevice_error}")
        return 1
    try:
        devices = sd.query_devices()
    except Exception as exc:  # pragma: no cover
        print(f"sounddevice query failed: {exc}")
        return 1

    found = 0
    for i, device in enumerate(devices):
        if not isinstance(device, dict):
            continue
        max_in = int(device.get("max_input_channels") or 0)
        if max_in > 0:
            hostapi = device.get("hostapi")
            default_sr = device.get("default_samplerate")
            print(f"[{i}] {device.get('name')} | in={max_in} | hostapi={hostapi} | default_sr={default_sr}")
            found += 1

    if found == 0:
        print("No input-capable devices found")
        return 1

    try:
        default_input = sd.default.device[0]
        print(f"Default input index: {default_input}")
    except Exception as exc:  # pragma: no cover
        print(f"Could not query default input index: {exc}")
    return 0


def check_cameras(config: dict) -> int:
    print("=== Camera sources ===")
    if cv2 is None:
        print(f"cv2 import failed: {cv2_error}")
        return 1

    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    open_count = 0
    if not by_id:
        print("(none) /dev/v4l/by-id entries")
    for path in by_id:
        try:
            target = os.path.realpath(path)
        except Exception:
            target = "?"
        ok = _try_open_camera(path)
        if ok:
            open_count += 1
        print(f"{path} -> {target} | open={ok} | tried={_candidate_sources(path)}")

    if open_count == 0:
        print("No camera sources opened successfully")
        return 1

    cameras = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras, list):
        cameras = []
    for idx, cam in enumerate(cameras):
        source = None
        if isinstance(cam, dict):
            source = cam.get("device_path") or cam.get("device_index", idx)
        else:
            source = idx
        print(f"Config camera[{idx}]: {source}")
    return 0


def check_cv2() -> int:
    if cv2 is None:
        print(f"cv2 import failed: {cv2_error}")
        return 1
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        np = None

    print(f"cv2={cv2.__version__}")
    print(f"numpy={getattr(np, '__version__', 'unknown')}")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FocusField Pi preflight checks")
    parser.add_argument("--config", default="configs/full_3cam_8mic_pi.yaml")
    args = parser.parse_args()

    print("FocusField Pi preflight")
    config = _safe_yaml_load(Path(args.config)) if args.config else {}
    if config:
        print(f"Loaded config: {args.config}")
    else:
        print(f"Config missing or empty: {args.config}")

    rc = 0
    rc |= check_cv2()
    rc |= check_audio()
    rc |= check_cameras(config)

    if rc:
        print("Preflight: FAILED")
    else:
        print("Preflight: OK")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
