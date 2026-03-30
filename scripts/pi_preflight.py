#!/usr/bin/env python3
"""Pi preflight check for FocusField hardware and runtime dependencies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

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

from focusfield.audio.devices import is_raw_array_device, is_raw_array_ready, list_input_devices, resolve_input_device_index
from focusfield.core.config import load_config
from focusfield.platform.hardware_probe import (
    collect_camera_sources,
    is_capture_node,
    normalize_camera_scope,
    source_matches_camera_scope,
    try_open_camera_any_backend,
)


def _safe_yaml_load(path: Path) -> dict:
    try:
        data = load_config(str(path))
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    if yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"Could not load config {path}: {exc}")
        return {}


def _load_mic_profile_yaw(config: dict) -> tuple[str, Optional[float]]:
    audio_cfg = config.get("audio", {}) if isinstance(config, dict) else {}
    if not isinstance(audio_cfg, dict):
        return "", None
    profile_name = str(audio_cfg.get("device_profile", "") or "").strip()
    if not profile_name:
        return "", None
    profiles_path = Path("configs/device_profiles.yaml")
    profiles = _safe_yaml_load(profiles_path)
    mic_arrays = profiles.get("mic_arrays", {}) if isinstance(profiles, dict) else {}
    if not isinstance(mic_arrays, dict):
        return profile_name, None
    profile = mic_arrays.get(profile_name)
    if not isinstance(profile, dict):
        return profile_name, None
    yaw = profile.get("yaw_offset_deg")
    runtime_yaw = audio_cfg.get("yaw_offset_deg")
    try:
        base_yaw = float(yaw)
    except Exception:
        base_yaw = None
    try:
        override_yaw = float(runtime_yaw)
    except Exception:
        override_yaw = 0.0
    if base_yaw is None:
        return profile_name, None
    return profile_name, float(base_yaw + override_yaw)


def _format_input_channels(config: dict) -> tuple[int | None, int, str]:
    selected_idx: int | None = None
    selected_channels = 0
    selected_name = ""
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
            selected_name = str(selected.name)
    if selected_idx is None and sd is not None:
        try:
            default_idx = sd.default.device[0]
            if default_idx is not None and int(default_idx) >= 0:
                selected_idx = int(default_idx)
                selected = next((d for d in inputs if d.index == selected_idx), None)
                if selected is not None:
                    selected_channels = int(selected.max_input_channels)
                    selected_name = str(selected.name)
        except Exception:
            selected_idx = None
    return selected_idx, selected_channels, selected_name


def _haar_cascade_available() -> bool:
    if cv2 is None:
        return False
    candidates = []
    if hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
        candidates.append(Path(str(cv2.data.haarcascades)) / "haarcascade_frontalface_default.xml")
    cv2_root = Path(cv2.__file__).resolve().parent if getattr(cv2, "__file__", None) else None
    if cv2_root is not None:
        candidates.extend(
            [
                cv2_root / "data" / "haarcascade_frontalface_default.xml",
                cv2_root.parent / "share" / "opencv4" / "haarcascades" / "haarcascade_frontalface_default.xml",
                Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml"),
            ]
        )
    for path in candidates:
        if not path.exists():
            continue
        try:
            cascade = cv2.CascadeClassifier(str(path))
        except Exception:
            continue
        if not cascade.empty():
            return True
    return False


def _camera_probe_source(cam: Any) -> tuple[Any, Any]:
    device_path = None
    device_index = None
    if isinstance(cam, dict):
        path = cam.get("device_path")
        if isinstance(path, str) and path.strip():
            device_path = path
        idx = cam.get("device_index")
        if isinstance(idx, int):
            device_index = idx
        elif isinstance(idx, str):
            try:
                device_index = int(idx)
            except (TypeError, ValueError):
                device_index = None
    return device_path, device_index


def _try_camera_open_with_fallback(
    source: Any,
    strict_capture: bool,
    camera_scope: str,
) -> tuple[bool, list[tuple[object, str]], tuple[object, str] | None]:
    if not isinstance(source, str):
        return try_open_camera_any_backend(
            source,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )

    ok, tried, opened = try_open_camera_any_backend(
        source,
        strict_capture=strict_capture,
        camera_scope=camera_scope,
    )
    if ok or not strict_capture:
        return ok, tried, opened
    # Legacy/non-capture nodes can legitimately appear as /dev/videoN symlinks on some boards.
    # In strict mode, still allow CAP_ANY as a safety fallback and surface success clearly.
    fallback_ok, fallback_tried, fallback_opened = try_open_camera_any_backend(
        source,
        strict_capture=False,
        camera_scope=camera_scope,
    )
    if fallback_ok:
        return fallback_ok, tried + fallback_tried, fallback_opened
    return False, tried, opened


def check_audio(config: dict) -> tuple[int, dict[str, Any]]:
    print("=== Audio inputs ===")
    details: dict[str, Any] = {
        "input_count": 0,
        "selected_index": None,
        "selected_channels": 0,
        "selected_name": "",
        "max_channels": 0,
        "uma8_raw_ready": False,
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

    selected_idx, selected_channels, selected_name = _format_input_channels(config)
    details["selected_index"] = selected_idx
    details["selected_channels"] = selected_channels
    details["selected_name"] = selected_name
    details["uma8_raw_ready"] = bool(is_raw_array_ready(selected_name, selected_channels, 8))
    print(f"Selected input index: {selected_idx}")
    print(f"Selected input name: {selected_name!r}")
    print(f"UMA-8 RAW 8ch ready: {details['uma8_raw_ready']}")
    return 0, details


def _source_capture_capable(path: str, camera_scope: str, strict_capture: bool = False) -> bool:
    if not source_matches_camera_scope(path, camera_scope=camera_scope):
        return False
    try:
        resolved = os.path.realpath(path)
    except Exception:
        resolved = path
    if not resolved.startswith("/dev/video"):
        return True
    capture = is_capture_node(resolved)
    if strict_capture:
        return capture is True
    return capture is not False


def check_cameras(config: dict, camera_source: str, strict_capture: bool, camera_scope: str) -> tuple[int, dict[str, Any]]:
    print("=== Camera sources ===")
    details: dict[str, Any] = {
        "scope": camera_scope,
        "discovered_sources": 0,
        "capture_capable_sources": 0,
        "openable_sources": 0,
        "configured_cameras": 0,
        "configured_openable": 0,
    }
    if cv2 is None:
        print(f"cv2 import failed: {cv2_error}")
        return 1, details

    camera_sources = collect_camera_sources(camera_source, camera_scope=camera_scope)
    details["discovered_sources"] = len(camera_sources)
    by_id_open_count = 0
    if not camera_sources:
        print("(none) camera sources discovered")
    for path in camera_sources:
        try:
            target = os.path.realpath(path)
        except Exception:
            target = "?"
        if _source_capture_capable(
            path,
            camera_scope=camera_scope,
            strict_capture=strict_capture,
        ):
            details["capture_capable_sources"] += 1
        ok, tried, opened = _try_camera_open_with_fallback(
            path,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
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
        device_path, device_index = _camera_probe_source(cam)
        source = device_index if device_path is None and device_index is not None else idx
        if isinstance(cam, dict):
            source = device_path if device_path is not None else source

        ok, tried, opened = _try_camera_open_with_fallback(
            source,
            strict_capture=strict_capture,
            camera_scope=camera_scope,
        )
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


def check_face_detector_backend(config: dict) -> tuple[int, dict[str, Any]]:
    print("=== Face detector backend ===")
    details: dict[str, Any] = {
        "requested_backend": "auto",
        "active_backend": "auto",
        "yunet_available": False,
        "haar_available": False,
        "degraded": False,
        "operational": True,
        "fallback_backend": "",
        "per_camera_active_backend": [],
    }
    if cv2 is None:
        details["degraded"] = True
        details["operational"] = False
        details["reason"] = f"cv2_import_failed:{cv2_error}"
        print(f"Face detector capability unavailable: {cv2_error}")
        return 0, details

    vision_cfg = config.get("vision", {}) if isinstance(config, dict) else {}
    face_cfg = vision_cfg.get("face", {}) if isinstance(vision_cfg, dict) else {}
    requested = str(face_cfg.get("backend", face_cfg.get("detector_backend", "auto")) or "auto").strip().lower()
    details["requested_backend"] = requested
    details["haar_available"] = bool(_haar_cascade_available())
    has_yunet = hasattr(cv2, "FaceDetectorYN_create")
    details["yunet_available"] = bool(has_yunet)
    if requested == "auto":
        if has_yunet:
            details["active_backend"] = "yunet"
            print("detector_backend=auto -> yunet")
        elif details["haar_available"]:
            details["active_backend"] = "haar"
            print("detector_backend=auto -> haar")
        else:
            details["active_backend"] = "none"
            details["degraded"] = True
            details["operational"] = False
            details["reason"] = "no_face_detector_backend"
            print("detector_backend=auto requested, but neither YuNet nor Haar cascade is available.")
    elif requested == "haar":
        details["active_backend"] = "haar" if details["haar_available"] else "none"
        details["operational"] = bool(details["haar_available"])
        if not details["haar_available"]:
            details["degraded"] = True
            details["reason"] = "haar_cascade_missing"
        print(f"detector_backend={requested} -> {details['active_backend']}")
    elif has_yunet:
        details["active_backend"] = "yunet"
        print(f"detector_backend={requested} -> yunet available")
    else:
        if details["haar_available"]:
            details["active_backend"] = "haar"
            details["degraded"] = True
            details["fallback_backend"] = "haar"
            details["reason"] = "facedetectoryn_unavailable"
            print(
                "detector_backend=yunet requested, but FaceDetectorYN is unavailable; "
                "runtime will fallback to haar (degraded recall)."
            )
        else:
            details["active_backend"] = "none"
            details["degraded"] = True
            details["operational"] = False
            details["reason"] = "no_face_detector_backend"
            print("detector_backend=yunet requested, but neither YuNet nor Haar cascade is available.")
    cameras = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if isinstance(cameras, list):
        details["per_camera_active_backend"] = [
            {
                "camera_id": str(cam.get("id", f"cam{idx}")),
                "active_backend": details["active_backend"],
                "requested_backend": requested,
                "degraded": bool(details["degraded"]),
            }
            for idx, cam in enumerate(cameras)
            if isinstance(cam, dict)
        ]
    return 0, details


def check_led_hid(require_led_hid: bool, vendor_id: int, product_id: int) -> tuple[int, dict[str, Any]]:
    print("=== UMA8 HID ===")
    details: dict[str, Any] = {
        "required": bool(require_led_hid),
        "vendor_id": int(vendor_id),
        "product_id": int(product_id),
        "hid_import_ok": False,
        "device_count": 0,
    }
    if not require_led_hid:
        print("HID check skipped (not required)")
        return 0, details

    try:
        import hid  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        print(f"HID import failed: {exc}")
        details["error"] = f"hid_import_failed:{exc}"
        return 1, details

    details["hid_import_ok"] = True
    try:
        devices = hid.enumerate(int(vendor_id), int(product_id))
    except Exception as exc:  # noqa: BLE001
        print(f"HID enumerate failed: {exc}")
        details["error"] = f"hid_enumerate_failed:{exc}"
        return 1, details
    device_count = len(devices)
    details["device_count"] = device_count
    print(f"HID enumerate vid=0x{int(vendor_id):04x} pid=0x{int(product_id):04x} count={device_count}")
    if device_count <= 0:
        details["error"] = "hid_no_device"
        return 1, details
    return 0, details


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FocusField Pi preflight checks")
    parser.add_argument("--config", default="configs/full_3cam_8mic_pi_prod.yaml")
    parser.add_argument(
        "--camera-source",
        choices=["auto", "by-path", "by-id", "index"],
        default="auto",
        help="Camera discovery mode for source-level diagnostics.",
    )
    parser.add_argument("--require-cameras", type=int, default=0)
    parser.add_argument("--require-audio-channels", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--camera-scope",
        choices=["usb", "any"],
        default=None,
        help="Camera hardware scope: usb for external UVC cameras only, any for all capture nodes.",
    )
    parser.add_argument("--require-led-hid", action="store_true")
    parser.add_argument("--audio-only", action="store_true", help="Skip camera and face-detector readiness checks.")
    parser.add_argument("--led-vendor-id", type=int, default=None)
    parser.add_argument("--led-product-id", type=int, default=None)
    parser.add_argument("--json-out", type=str, default="", help="Optional JSON output path for machine-readable preflight report.")
    args = parser.parse_args()

    print("FocusField Pi preflight")
    config = _safe_yaml_load(Path(args.config)) if args.config else {}
    config_loaded = bool(config)
    if config_loaded:
        print(f"Loaded config: {args.config}")
    else:
        print(f"Config missing or empty: {args.config}")

    rc = 0
    req_cfg = config.get("runtime", {}).get("requirements", {}) if isinstance(config, dict) else {}
    configured_scope = req_cfg.get("camera_scope") if isinstance(req_cfg, dict) else None
    camera_scope = normalize_camera_scope(
        args.camera_scope or configured_scope or ("usb" if args.strict else "any")
    )

    if not args.audio_only:
        rc |= check_cv2()
    if args.audio_only:
        face_backend_details = {
            "requested_backend": "skipped",
            "active_backend": "skipped",
            "yunet_available": False,
            "haar_available": False,
            "degraded": False,
            "operational": True,
            "fallback_backend": "",
            "per_camera_active_backend": [],
            "skipped": True,
        }
    else:
        face_backend_rc, face_backend_details = check_face_detector_backend(config)
        rc |= face_backend_rc
    audio_rc, audio_details = check_audio(config)
    rc |= audio_rc
    if args.audio_only:
        camera_details = {
            "scope": camera_scope,
            "discovered_sources": 0,
            "capture_capable_sources": 0,
            "openable_sources": 0,
            "configured_cameras": 0,
            "configured_openable": 0,
            "skipped": True,
        }
    else:
        camera_rc, camera_details = check_cameras(
            config,
            camera_source=args.camera_source,
            strict_capture=args.strict,
            camera_scope=camera_scope,
        )
        rc |= camera_rc

    uma8_cfg = config.get("uma8_leds", {}) if isinstance(config, dict) else {}
    if not isinstance(uma8_cfg, dict):
        uma8_cfg = {}
    required_led_hid_cfg = False
    if isinstance(req_cfg, dict):
        required_led_hid_cfg = bool(req_cfg.get("require_led_hid", False))
    require_led_hid = bool(args.require_led_hid or required_led_hid_cfg)
    vendor_id = int(args.led_vendor_id if args.led_vendor_id is not None else int(uma8_cfg.get("vendor_id", 0x2752) or 0x2752))
    product_id = int(args.led_product_id if args.led_product_id is not None else int(uma8_cfg.get("product_id", 0x001C) or 0x001C))
    led_rc, led_details = check_led_hid(require_led_hid=require_led_hid, vendor_id=vendor_id, product_id=product_id)
    rc |= led_rc

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
        selected_name = str(audio_details.get("selected_name", ""))
        hint = ""
        if is_raw_array_device(selected_name) and required_audio_channels >= 8:
            hint = " hint: the raw array device appears to be in 2ch mode; switch to RAW firmware for 8ch."
        contract_failures.append(
            f"required audio_channels={required_audio_channels} but selected channels={observed_audio_channels} "
            f"(index={audio_details.get('selected_index')}, name={selected_name!r}){hint}"
        )
    if require_led_hid and int(led_details.get("device_count", 0) or 0) <= 0:
        contract_failures.append(
            "required UMA8 HID device not available "
            f"(vid=0x{vendor_id:04x}, pid=0x{product_id:04x})"
        )
    if not args.audio_only and not bool(face_backend_details.get("operational", False)):
        contract_failures.append("no operational face detector backend available")

    print("=== Alignment Summary ===")
    cameras_cfg = config.get("video", {}).get("cameras", []) if isinstance(config, dict) else []
    if not isinstance(cameras_cfg, list):
        cameras_cfg = []
    camera_yaws = []
    camera_yaw_map: list[dict[str, Any]] = []
    for idx, cam in enumerate(cameras_cfg):
        if not isinstance(cam, dict):
            continue
        cam_id = str(cam.get("id", f"cam{idx}"))
        try:
            yaw = float(cam.get("yaw_offset_deg", 0.0) or 0.0)
        except Exception:
            yaw = 0.0
        camera_yaws.append(f"{cam_id}:{yaw:.1f}")
        camera_yaw_map.append({"id": cam_id, "yaw_offset_deg": yaw})
    camera_yaw_map_text = ", ".join(camera_yaws) if camera_yaws else "n/a"
    print("camera_yaw_map=" + camera_yaw_map_text)
    overlay_cfg = config.get("runtime", {}).get("camera_calibration_overlay", {}) if isinstance(config, dict) else {}
    if isinstance(overlay_cfg, dict):
        print(
            "camera_calibration_overlay="
            f"active={bool(overlay_cfg.get('active', False))} "
            f"path={overlay_cfg.get('path', '')}"
        )
    profile_name, mic_yaw = _load_mic_profile_yaw(config)
    if profile_name:
        if mic_yaw is None:
            print(f"mic_profile={profile_name} yaw_offset_deg=n/a")
        else:
            print(f"mic_profile={profile_name} yaw_offset_deg={mic_yaw:.1f}")
    alignment_cfg = config.get("runtime", {}).get("alignment", {}) if isinstance(config, dict) else {}
    if not isinstance(alignment_cfg, dict):
        alignment_cfg = {}
    require_calibrated_mic_yaw = bool(alignment_cfg.get("require_calibrated_mic_yaw", False))
    alignment_warnings: list[str] = []
    if require_calibrated_mic_yaw and mic_yaw is not None and abs(float(mic_yaw)) < 1e-6:
        warning = "runtime.alignment.require_calibrated_mic_yaw=true but mic yaw_offset_deg is 0.0"
        alignment_warnings.append(warning)
        print(f"alignment_warning: {warning}")

    print("=== Contract Summary ===")
    print(f"strict={args.strict}")
    print(f"audio_only={args.audio_only}")
    print(f"camera_scope={camera_scope}")
    print(f"required_cameras={required_cameras} observed_cameras={observed_cameras}")
    print(
        f"required_audio_channels={required_audio_channels} observed_audio_channels={observed_audio_channels}"
    )
    print(
        "face_backend_requested="
        f"{face_backend_details.get('requested_backend')} "
        f"active={face_backend_details.get('active_backend')} "
        f"degraded={bool(face_backend_details.get('degraded', False))}"
    )
    print(
        f"face_backend_operational={bool(face_backend_details.get('operational', False))} "
        f"haar_available={bool(face_backend_details.get('haar_available', False))} "
        f"yunet_available={bool(face_backend_details.get('yunet_available', False))}"
    )
    print(f"require_led_hid={require_led_hid} observed_hid_devices={int(led_details.get('device_count', 0) or 0)}")
    print(f"uma8_raw_ready={bool(audio_details.get('uma8_raw_ready', False))}")
    if contract_failures:
        for item in contract_failures:
            print(f"contract_failure: {item}")
    if args.strict and contract_failures:
        rc |= 1

    preflight_report: Dict[str, Any] = {
        "config_loaded": bool(config_loaded),
        "config_path": str(args.config or ""),
        "strict": bool(args.strict),
        "audio_only": bool(args.audio_only),
        "camera_scope": str(camera_scope),
        "face_detector": {
            "requested_backend": face_backend_details.get("requested_backend"),
            "active_backend": face_backend_details.get("active_backend"),
            "degraded": bool(face_backend_details.get("degraded", False)),
            "reason": face_backend_details.get("reason", ""),
            "operational": bool(face_backend_details.get("operational", False)),
            "haar_available": bool(face_backend_details.get("haar_available", False)),
            "yunet_available": bool(face_backend_details.get("yunet_available", False)),
            "fallback_backend": face_backend_details.get("fallback_backend", ""),
            "per_camera_active_backend": face_backend_details.get("per_camera_active_backend", []),
        },
        "camera_contract": {
            "required": int(required_cameras),
            "observed": int(observed_cameras),
            "passed": bool(required_cameras <= 0 or observed_cameras >= required_cameras),
            "details": camera_details,
        },
        "audio_contract": {
            "required_channels": int(required_audio_channels),
            "observed_channels": int(observed_audio_channels),
            "passed": bool(required_audio_channels <= 0 or observed_audio_channels >= required_audio_channels),
            "details": audio_details,
            "uma8_raw_ready": bool(audio_details.get("uma8_raw_ready", False)),
        },
        "led_hid_contract": {
            "required": bool(require_led_hid),
            "vendor_id": int(vendor_id),
            "product_id": int(product_id),
            "passed": bool((not require_led_hid) or int(led_details.get("device_count", 0) or 0) > 0),
            "details": led_details,
        },
        "alignment_summary": {
            "camera_yaw_map": camera_yaw_map,
            "camera_yaw_map_text": camera_yaw_map_text,
            "camera_calibration_overlay": overlay_cfg if isinstance(overlay_cfg, dict) else {},
            "mic_profile": profile_name,
            "mic_yaw_offset_deg": mic_yaw,
            "require_calibrated_mic_yaw": bool(require_calibrated_mic_yaw),
            "warnings": alignment_warnings,
        },
        "contract_failures": contract_failures,
        "preflight_ok": bool(rc == 0),
    }

    if args.json_out:
        out_path = Path(args.json_out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(preflight_report, handle, indent=2, sort_keys=True)

    if rc:
        print("Preflight: FAILED")
    else:
        print("Preflight: OK")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
