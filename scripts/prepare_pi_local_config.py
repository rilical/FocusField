#!/usr/bin/env python3
"""Generate a local Pi config from currently attached devices.

This avoids manual YAML editing and picks a compatible audio profile when the
connected microphones don't match the full UMA-8 profile.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import sounddevice as sd
import yaml


def candidate_sources(source: str) -> List[str]:
    """Return fallback camera paths for OpenCV probing."""
    sources: List[str] = []
    if source.startswith("/dev/video"):
        sources.append(source)

    try:
        resolved = os.path.realpath(source)
    except Exception:
        resolved = None
    else:
        if resolved and resolved != source:
            if resolved not in sources:
                sources.append(resolved)
            m = re.search(r"/dev/video(\d+)$", resolved)
            if m is not None:
                video_source = f"/dev/video{m.group(1)}"
                if video_source not in sources:
                    sources.append(video_source)
            if source not in sources:
                sources.append(source)

    if source not in sources:
        sources.append(source)

    # Keep this deterministic and short.
    return list(dict.fromkeys(sources))


def can_open_v4l2(path: str) -> Tuple[bool, Optional[str]]:
    """Return whether OpenCV can open `path` via preferred backends."""
    backends = (
        cv2.CAP_V4L2,
        cv2.CAP_ANY,
    )
    for candidate in candidate_sources(path):
        for backend in backends:
            cap = cv2.VideoCapture(candidate, backend)
            if cap.isOpened():
                ok, _ = cap.read()
                cap.release()
                if ok:
                    return True, candidate
            cap.release()
    return False, None


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


def detect_cameras(limit: int) -> List[Tuple[str, str]]:
    cameras: List[Tuple[str, str]] = []
    sources: List[str] = sorted(glob.glob("/dev/v4l/by-id/*"))
    if not sources:
        sources = sorted(path for path in glob.glob("/dev/video*") if re.match(r"^/dev/video\\d+$", path))

    for by_id in sources:
        opened, opened_source = can_open_v4l2(by_id)
        if not opened or opened_source is None:
            continue
        cameras.append((by_id, opened_source))
        if len(cameras) >= limit:
            break
    return cameras


def detect_audio() -> Tuple[Optional[int], int, str]:
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


def build_video_entries(
    base_cameras: List[dict],
    camera_sources: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for i, (device_path, _opened_path) in enumerate(camera_sources):
        if i >= len(base_cameras):
            break
        base = dict(base_cameras[i])
        base.update(
            {
                "device_path": device_path,
                "device_index": i,
            }
        )
        result.append(base)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-config",
        default="configs/full_3cam_8mic_pi.yaml",
        help="Template config to patch.",
    )
    parser.add_argument(
        "--output",
        default="configs/full_3cam_working_local.yaml",
        help="Output path for hardware-adaptive config.",
    )
    parser.add_argument(
        "--max-cameras",
        type=int,
        default=3,
        help="How many cameras to include in output.",
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

    cameras = detect_cameras(args.max_cameras)
    if not cameras:
        raise SystemExit("No working cameras found via CAP_V4L2 probe.")

    audio_index, audio_channels, audio_name = detect_audio()
    if audio_index is None:
        raise SystemExit("No input audio device found.")

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

    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    print(f"wrote: {output_path}")
    print(
        f"cameras_used={len(cameras)} channels={audio_channels} audio_idx={audio_index} profile={audio_cfg['device_profile']}"
    )
    print(f"selected_audio={audio_name!r} default_ch={audio_channels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
