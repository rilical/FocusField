#!/usr/bin/env python3
"""Generate a local Pi config from currently attached devices."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import sounddevice as sd
import yaml

from focusfield.platform.hardware_probe import (
    collect_camera_sources,
    is_capture_node,
    try_open_camera_any_backend,
    video_index_for_source,
)


def load_profiles(config_root: Path) -> dict:
    with (config_root / "configs/device_profiles.yaml").open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def pick_profile_name(channels: int, profiles: Dict[str, Any], preferred: str) -> str:
    mic_arrays = profiles.get("mic_arrays", {}) if isinstance(profiles, dict) else {}
    if not isinstance(mic_arrays, dict):
        return preferred

    def profile_len(name: str) -> Optional[int]:
        profile = mic_arrays.get(name)
        if not isinstance(profile, dict):
            return None
        order = profile.get("channel_order")
        return len(order) if isinstance(order, list) else None

    if preferred in mic_arrays:
        preferred_len = profile_len(preferred)
        if preferred_len == channels:
            return preferred

    for candidate in (
        "mic_mono_default",
        "mic_array_2ch_default",
        "mic_array_4ch_default",
        "mic_array_6ch_default",
        "minidsp_uma8_raw_7p1",
    ):
        if profile_len(candidate) == channels:
            return candidate

    return preferred


def parse_camera_indices(values: Optional[List[str]]) -> List[int]:
    if not values:
        return []
    result: List[int] = []
    for value in values:
        if not value:
            continue
        for token in str(value).replace(",", " ").split():
            token = token.strip()
            if not token:
                continue
            if token.startswith("/dev/video"):
                token = token.replace("/dev/video", "", 1).strip()
            elif token.startswith("video"):
                token = token[5:].strip()
            try:
                idx = int(token)
            except ValueError:
                continue
            if idx < 0:
                continue
            if idx not in result:
                result.append(idx)
    return result


def _camera_capture_capable(source: str) -> bool:
    try:
        resolved = os.path.realpath(source)
    except Exception:
        resolved = source
    if not resolved.startswith("/dev/video"):
        return True
    capture = is_capture_node(resolved)
    return capture is not False


def detect_cameras(limit: int, camera_source: str, strict_capture: bool) -> tuple[list[tuple[str, object]], dict]:
    cameras: list[tuple[str, object]] = []
    discovered = collect_camera_sources(camera_source)
    capture_capable = 0
    openable = 0

    for source in discovered:
        if _camera_capture_capable(source):
            capture_capable += 1
        elif camera_source == "auto" or strict_capture:
            continue

        ok, _, opened = try_open_camera_any_backend(source, strict_capture=strict_capture)
        if not ok or opened is None:
            continue
        openable += 1
        cameras.append((source, opened[0]))
        if len(cameras) >= limit:
            break

    stats = {
        "discovered_sources": len(discovered),
        "capture_capable_sources": capture_capable,
        "openable_sources": openable,
    }
    return cameras, stats


def _camera_paths_from_indices(indices: List[int], limit: int, strict: bool) -> tuple[list[tuple[str, int]], list[str]]:
    cameras: list[tuple[str, int]] = []
    failures: list[str] = []
    seen: set[int] = set()
    for idx in indices:
        if idx in seen:
            continue
        if limit >= 0 and len(cameras) >= limit:
            break
        seen.add(idx)
        path = f"/dev/video{idx}"
        if not os.path.exists(path):
            failures.append(f"explicit camera index {idx} does not exist ({path})")
            continue

        ok, _, opened = try_open_camera_any_backend(path, strict_capture=strict)
        if ok and opened is not None:
            opened_source = opened[0]
            if isinstance(opened_source, int):
                cameras.append((path, opened_source))
            elif isinstance(opened_source, str):
                opened_index = video_index_for_source(opened_source)
                cameras.append((path, opened_index if opened_index is not None else idx))
            else:
                cameras.append((path, idx))
            continue

        if strict:
            failures.append(f"explicit camera index {idx} ({path}) failed strict open probe")
        else:
            cameras.append((path, idx))
            failures.append(
                f"explicit camera index {idx} ({path}) not opened during probe; added for manual validation"
            )
    return cameras, failures


def detect_audio() -> tuple[Optional[int], int, str]:
    selected_index = None
    selected_channels = 0
    selected_name = ""

    for i, device in enumerate(sd.query_devices()):
        max_in = int(device.get("max_input_channels") or 0)
        if max_in <= 0:
            continue
        name = str(device.get("name", ""))
        if selected_index is None or (selected_name.find("miniDSP") < 0 and "miniDSP" in name):
            selected_index = i
            selected_channels = max_in
            selected_name = name
        if "miniDSP" in name and max_in >= 8:
            selected_index = i
            selected_channels = max_in
            selected_name = name
            break

    if selected_index is None:
        return None, 0, ""
    return selected_index, selected_channels, selected_name


def build_video_entries(base_cameras: List[dict], camera_sources: List[tuple[str, object]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for i, (device_path, opened_path) in enumerate(camera_sources):
        if i >= len(base_cameras):
            break
        base = dict(base_cameras[i])
        opened_index: Optional[int]
        if isinstance(opened_path, int):
            opened_index = opened_path
        elif isinstance(opened_path, str):
            opened_index = video_index_for_source(opened_path)
        else:
            opened_index = None

        if opened_index is None and isinstance(device_path, str):
            opened_index = video_index_for_source(device_path)

        base.update(
            {
                "device_path": device_path,
                "device_index": opened_index if opened_index is not None else i,
            }
        )
        result.append(base)
    return result


def _apply_runtime_requirements(cfg: Dict[str, Any], strict: bool, min_cameras: int, min_audio_channels: int) -> None:
    runtime_cfg = cfg.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        cfg["runtime"] = runtime_cfg
    req = runtime_cfg.get("requirements", {})
    if not isinstance(req, dict):
        req = {}
    req["strict"] = bool(strict)
    req["min_cameras"] = int(max(0, min_cameras))
    req["min_audio_channels"] = int(max(0, min_audio_channels))
    runtime_cfg["requirements"] = req
    if strict:
        runtime_cfg["fail_fast"] = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-config", default="configs/full_3cam_8mic_pi.yaml", help="Template config to patch.")
    parser.add_argument(
        "--output",
        default="configs/full_3cam_working_local.yaml",
        help="Output path for hardware-adaptive config.",
    )
    parser.add_argument("--max-cameras", type=int, default=3, help="How many cameras to include in output.")
    parser.add_argument(
        "--camera-source",
        choices=["auto", "by-path", "by-id", "index"],
        default="auto",
        help="Camera discovery mode. auto prefers by-path then by-id then /dev/videoN.",
    )
    parser.add_argument(
        "--camera-indices",
        nargs="+",
        default=None,
        help="Explicit video indices to force into config (e.g. --camera-indices 0 1 2).",
    )
    parser.add_argument("--require-cameras", type=int, default=0, help="Minimum required openable cameras.")
    parser.add_argument(
        "--require-audio-channels",
        type=int,
        default=0,
        help="Minimum required input channels on selected audio device.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce contract requirements and fail without writing output when unmet.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    base_path = repo_root / args.base_config
    output_path = Path(args.output)
    if output_path.is_relative_to(Path.cwd()):
        output_path = (Path.cwd() / output_path).resolve()
    elif not output_path.is_absolute():
        output_path = (repo_root / output_path).resolve()

    if not output_path.parent.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)

    with base_path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    if not isinstance(cfg, dict):
        raise SystemExit("Base config missing or not a mapping.")

    explicit_indices = parse_camera_indices(args.camera_indices)
    discovery_stats = {"discovered_sources": 0, "capture_capable_sources": 0, "openable_sources": 0}
    notices: list[str] = []
    if explicit_indices:
        cameras, notices = _camera_paths_from_indices(explicit_indices, args.max_cameras, strict=args.strict)
        discovery_stats["discovered_sources"] = len(explicit_indices)
        discovery_stats["capture_capable_sources"] = len(cameras)
        discovery_stats["openable_sources"] = len(cameras)
        if not cameras:
            raise SystemExit("No explicit camera indices resolved.")
    else:
        cameras, discovery_stats = detect_cameras(
            args.max_cameras,
            camera_source=args.camera_source,
            strict_capture=args.strict,
        )
        if not cameras:
            raise SystemExit("No working cameras found via probe.")

    audio_index, audio_channels, audio_name = detect_audio()
    if audio_index is None:
        raise SystemExit("No input audio device found.")

    contract_errors: list[str] = []
    req_cams = int(max(0, args.require_cameras))
    req_audio = int(max(0, args.require_audio_channels))
    if req_cams > 0 and len(cameras) < req_cams:
        contract_errors.append(
            f"required cameras={req_cams} but openable cameras={len(cameras)} "
            f"(discovered={discovery_stats['discovered_sources']}, capture_capable={discovery_stats['capture_capable_sources']})"
        )
    if req_audio > 0 and audio_channels < req_audio:
        contract_errors.append(
            f"required audio_channels={req_audio} but selected device has channels={audio_channels} "
            f"(index={audio_index}, name={audio_name!r})"
        )
    if args.strict and contract_errors:
        joined = "\n".join(f"- {err}" for err in contract_errors)
        raise SystemExit(f"Strict contract check failed:\n{joined}")

    profiles = load_profiles(repo_root)
    audio_cfg = cfg.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
        cfg["audio"] = audio_cfg

    current_profile = str(audio_cfg.get("device_profile", "mic_array_8ch_default"))
    audio_cfg["channels"] = audio_channels
    audio_cfg["device_profile"] = pick_profile_name(audio_channels, profiles, current_profile)
    selector = dict(audio_cfg.get("device_selector", {}) or {})
    selector["require_input_channels"] = audio_channels
    selector["match_substring"] = audio_name if audio_channels < 8 else "miniDSP"
    audio_cfg["device_selector"] = selector
    capture = dict(audio_cfg.get("capture", {}) or {})
    capture["allow_mono_fallback"] = audio_channels <= 1
    audio_cfg["capture"] = capture

    video_cfg = cfg.get("video", {})
    if isinstance(video_cfg, dict):
        base_cameras = video_cfg.get("cameras", [])
        if isinstance(base_cameras, list):
            video_cfg["cameras"] = build_video_entries(
                [camera for camera in base_cameras if isinstance(camera, dict)],
                cameras,
            )
        else:
            video_cfg["cameras"] = []

    ui_cfg = cfg.get("ui", {})
    if isinstance(ui_cfg, dict):
        ui_cfg["host"] = "0.0.0.0"

    _apply_runtime_requirements(
        cfg,
        strict=args.strict,
        min_cameras=req_cams,
        min_audio_channels=req_audio,
    )

    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    print(f"wrote: {output_path}")
    print(
        f"cameras_used={len(cameras)} channels={audio_channels} audio_idx={audio_index} "
        f"profile={audio_cfg['device_profile']}"
    )
    print(f"selected_audio={audio_name!r} default_ch={audio_channels}")
    print(
        "camera_probe: "
        f"source_mode={args.camera_source} discovered={discovery_stats['discovered_sources']} "
        f"capture_capable={discovery_stats['capture_capable_sources']} openable={discovery_stats['openable_sources']}"
    )
    for notice in notices:
        print(f"notice: {notice}")
    if contract_errors:
        for err in contract_errors:
            print(f"contract_warning: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
