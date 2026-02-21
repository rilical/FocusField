#!/usr/bin/env python3
"""Pi preflight check for FocusField hardware and runtime dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

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

from focusfield.audio.devices import list_input_devices, resolve_input_device_index
from focusfield.platform.hardware_probe import collect_camera_sources, is_capture_node, try_open_camera_any_backend


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


def _format_input_channels(config: dict) -> tuple[int | None, int]:
    selected_idx: int | None = None
    selected_channels = 0
    try:
        if config:
            selected_idx = resolve_input_device_index(config, logger=None)
    except Exception:
        selected_idx = None

    inputs = list_input_devices()
    if selected_idx is not None:
        selected = next((d for d in inputs if d.index == selected_idx), None)
        if selected is not None:
            selected_channels = int(selected.max_input_channels)
    if selected_idx is None and sd is not None:
        try:
            default_idx = sd.default.device[0]
            if default_idx is not None and int(default_idx) >= 0:
                selected_idx = int(default_idx)
                selected = next((d for d in inputs if d.index == selected_idx), None)
                if selected is not None:
                    selected_channels = int(selected.max_input_channels)
        except Exception:
            selected_idx = None
    return selected_idx, selected_channels


def check_audio(config: dict) -> tuple[int, dict[str, Any]]:
    print("=== Audio inputs ===")
    details: dict[str, Any] = {
        "input_count": 0,
        "selected_index": None,
        "selected_channels": 0,
        "max_channels": 0,
    }
    if sd is None:
        print(f"sounddevice import failed: {sounddevice_error}")
        return 1, details
    try:
        devices = list_input_devices()
    except Exception as exc:  # pragma: no cover
        print(f"sounddevice query failed: {exc}")
        return 1, details

    found = 0
    max_channels = 0
    for device in devices:
        max_in = int(device.max_input_channels)
        print(
            f"[{device.index}] {device.name} | in={max_in} | hostapi={device.hostapi} | "
            f"default_sr={device.default_samplerate_hz}"
        )
        found += 1
        max_channels = max(max_channels, max_in)

    if found == 0:
        print("No input-capable devices found")
        return 1, details
    details["input_count"] = found
    details["max_channels"] = max_channels

    selected_idx, selected_channels = _format_input_channels(config)
    details["selected_index"] = selected_idx
    details["selected_channels"] = selected_channels
    print(f"Selected input index: {selected_idx}")
    return 0, details


def _source_capture_capable(path: str) -> bool:
    try:
        resolved = os.path.realpath(path)
    except Exception:
        resolved = path
    if not resolved.startswith("/dev/video"):
        return True
    capture = is_capture_node(resolved)
    return capture is not False


def check_cameras(config: dict, camera_source: str, strict_capture: bool) -> tuple[int, dict[str, Any]]:
    print("=== Camera sources ===")
    details: dict[str, Any] = {
        "discovered_sources": 0,
        "capture_capable_sources": 0,
        "openable_sources": 0,
        "configured_cameras": 0,
        "configured_openable": 0,
    }
    if cv2 is None:
        print(f"cv2 import failed: {cv2_error}")
        return 1, details

    camera_sources = collect_camera_sources(camera_source)
    details["discovered_sources"] = len(camera_sources)
    by_id_open_count = 0
    if not camera_sources:
        print("(none) camera sources discovered")
    for path in camera_sources:
        try:
            target = os.path.realpath(path)
        except Exception:
            target = "?"
        if _source_capture_capable(path):
            details["capture_capable_sources"] += 1
        ok, tried, opened = try_open_camera_any_backend(path, strict_capture=strict_capture)
        backend = opened[1] if opened is not None else "none"
        if ok:
            by_id_open_count += 1
            details["openable_sources"] += 1
        print(f"{path} -> {target} | open={ok} | backend={backend} | tried={tried}")

    if by_id_open_count == 0:
        print("No camera sources opened successfully")
        return 1, details

    cameras = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras, list):
        cameras = []
    details["configured_cameras"] = len(cameras)
    config_open_count = 0
    for idx, cam in enumerate(cameras):
        source = idx
        if isinstance(cam, dict):
            source = cam.get("device_path") or cam.get("device_index", idx)
        ok, tried, opened = try_open_camera_any_backend(source, strict_capture=strict_capture)
        backend = opened[1] if opened is not None else "none"
        if ok:
            config_open_count += 1
        print(f"Config camera[{idx}]: {source} | open={ok} | backend={backend} | tried={tried}")

    details["configured_openable"] = config_open_count
    if config and config_open_count < len(cameras):
        print(f"Configured camera open failures: {len(cameras)-config_open_count}/{len(cameras)}")
        return 1, details
    return 0, details


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
    parser.add_argument(
        "--camera-source",
        choices=["auto", "by-path", "by-id", "index"],
        default="auto",
        help="Camera discovery mode for source-level diagnostics.",
    )
    parser.add_argument("--require-cameras", type=int, default=0)
    parser.add_argument("--require-audio-channels", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    print("FocusField Pi preflight")
    config = _safe_yaml_load(Path(args.config)) if args.config else {}
    if config:
        print(f"Loaded config: {args.config}")
    else:
        print(f"Config missing or empty: {args.config}")

    rc = 0
    rc |= check_cv2()
    audio_rc, audio_details = check_audio(config)
    rc |= audio_rc
    camera_rc, camera_details = check_cameras(config, camera_source=args.camera_source, strict_capture=args.strict)
    rc |= camera_rc

    required_cameras = int(max(0, args.require_cameras))
    required_audio_channels = int(max(0, args.require_audio_channels))
    configured_cameras = int(camera_details.get("configured_cameras") or 0)
    if configured_cameras > 0:
        observed_cameras = int(camera_details.get("configured_openable") or 0)
    else:
        observed_cameras = int(camera_details.get("openable_sources") or 0)
    observed_audio_channels = int(audio_details.get("selected_channels") or 0)
    contract_failures: list[str] = []
    if required_cameras > 0 and observed_cameras < required_cameras:
        contract_failures.append(
            f"required cameras={required_cameras} but observed openable cameras={observed_cameras}"
        )
    if required_audio_channels > 0 and observed_audio_channels < required_audio_channels:
        contract_failures.append(
            f"required audio_channels={required_audio_channels} but selected channels={observed_audio_channels}"
        )

    print("=== Contract Summary ===")
    print(f"strict={args.strict}")
    print(f"required_cameras={required_cameras} observed_cameras={observed_cameras}")
    print(
        f"required_audio_channels={required_audio_channels} observed_audio_channels={observed_audio_channels}"
    )
    if contract_failures:
        for item in contract_failures:
            print(f"contract_failure: {item}")
    if args.strict and contract_failures:
        rc |= 1

    if rc:
        print("Preflight: FAILED")
    else:
        print("Preflight: OK")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
