#!/usr/bin/env python3
"""Pi preflight check for FocusField hardware and runtime dependencies."""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Optional

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


_V4L2_CAPTURE_BITS = (0x00000001, 0x00001000, 0x0000000200, 0x0000080000)


def _video_index_for_source(source: str) -> int | None:
    match = re.search(r"/dev/video(\d+)$", source)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _is_capture_node(source: str) -> bool | None:
    index = _video_index_for_source(source)
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


def _candidate_sources(source: object) -> list[object]:
    if not isinstance(source, str):
        return [source]

    sources: list[object] = []
    source_is_video = source.startswith("/dev/video")
    source_is_by_id = source.startswith("/dev/v4l/by-id/")

    if source_is_video and _is_capture_node(source) is False:
        return []
    if source_is_video:
        sources.append(source)

    resolved: str | None = None
    try:
        resolved = os.path.realpath(source)
    except Exception:  # noqa: BLE001
        resolved = None

    if resolved and resolved != source:
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
            return sources

        if resolved not in sources:
            sources.append(resolved)

    if not source_is_by_id or resolved is None:
        if source not in sources:
            sources.append(source)
    if not source_is_video and source not in sources:
        sources.append(source)

    if source_is_video and sources == [source] and _is_capture_node(source) is False:
        return []

    return list(dict.fromkeys(sources))


def _try_open_camera(source: object) -> bool:
    ok, _, _ = _try_open_camera_any_backend(source)
    return ok


def _backend_to_source_key(source: object) -> object:
    if not isinstance(source, str):
        return source
    match = re.search(r"/dev/video(\d+)$", source)
    if match is None:
        return source
    try:
        return int(match.group(1))
    except Exception:
        return source


def _try_open_camera_any_backend(source: object) -> tuple[bool, list[tuple[object, str]], Optional[tuple[object, str]]]:
    if cv2 is None:
        return False, [], None
    backends = [
        ("CAP_V4L2", cv2.CAP_V4L2),
        ("CAP_ANY", cv2.CAP_ANY),
    ]
    tried: list[tuple[object, str]] = []
    for candidate in _candidate_sources(source):
        source_for_open = _backend_to_source_key(candidate)
        for backend_name, backend in backends:
            tried.append((candidate, backend_name))
            cap = cv2.VideoCapture(source_for_open, backend)
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    cap.release()
                    return True, tried, (candidate, backend_name)
            cap.release()
    return False, tried, None


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
    by_id_open_count = 0
    if not by_id:
        print("(none) /dev/v4l/by-id entries")
    for path in by_id:
        try:
            target = os.path.realpath(path)
        except Exception:
            target = "?"
        ok, tried, opened = _try_open_camera_any_backend(path)
        backend = opened[1] if opened is not None else "none"
        if ok:
            by_id_open_count += 1
        print(f"{path} -> {target} | open={ok} | backend={backend} | tried={tried}")

    if by_id_open_count == 0:
        print("No camera sources opened successfully")
        return 1

    cameras = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras, list):
        cameras = []
    config_open_count = 0
    for idx, cam in enumerate(cameras):
        source = idx
        if isinstance(cam, dict):
            source = cam.get("device_path") or cam.get("device_index", idx)
        ok, tried, opened = _try_open_camera_any_backend(source)
        backend = opened[1] if opened is not None else "none"
        if ok:
            config_open_count += 1
        print(f"Config camera[{idx}]: {source} | open={ok} | backend={backend} | tried={tried}")

    if config and config_open_count < len(cameras):
        print(f"Configured camera open failures: {len(cameras)-config_open_count}/{len(cameras)}")
        return 1
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
