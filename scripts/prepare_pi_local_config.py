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

_V4L2_CAPTURE_BITS = (0x00000001, 0x00001000)


def _video_index_for_path(path: str) -> Optional[int]:
    m = re.search(r"/dev/video(\d+)$", path)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _is_capture_node(path: str) -> bool | None:
    index = _video_index_for_path(path)
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


def candidate_sources(source: str) -> List[object]:
    """Return fallback camera paths for OpenCV probing."""
    sources: List[object] = []
    source_is_video = source.startswith("/dev/video")
    source_is_by_id = source.startswith("/dev/v4l/by-id/")
    if source_is_video:
        sources.append(source)

    try:
        resolved = os.path.realpath(source)
    except Exception:
        resolved = None
    else:
        if resolved and resolved != source:
            if resolved.startswith("/dev/video"):
                if _is_capture_node(resolved) is not False:
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

    if source_is_video and _is_capture_node(source) is False:
        source_index = _video_index_for_path(source)
        if source_index is not None and source_index not in sources:
            sources.append(source_index)

    # Keep this deterministic and short.
    return list(dict.fromkeys(sources))


def can_open_v4l2(path: str) -> Tuple[bool, Optional[object]]:
    """Return whether OpenCV can open `path` via preferred backends."""
    backends = (
        cv2.CAP_V4L2,
        cv2.CAP_ANY,
    )
    candidates = candidate_sources(path)
    if not candidates:
        return False, None
    for candidate in candidates:
        index = _video_index_for_path(candidate if isinstance(candidate, str) else str(candidate))
        source_obj: str | int = index if index is not None else candidate
        for backend in backends:
            cap = cv2.VideoCapture(source_obj, backend)
            if cap.isOpened():
                cap.release()
                return True, index if index is not None else candidate
            cap.release()
    return False, None


def _dedupe_paths(paths: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in paths:
        try:
            resolved = os.path.realpath(item)
        except Exception:
            resolved = item
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(item)
    return out


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


def detect_cameras(limit: int) -> List[Tuple[str, object]]:
    cameras: List[Tuple[str, object]] = []
    by_id_sources: List[str] = sorted(glob.glob("/dev/v4l/by-id/*"))
    video_node_sources: List[str] = sorted(
        path for path in glob.glob("/dev/video*") if re.match(r"^/dev/video\\d+$", path)
    )
    sources = _dedupe_paths(by_id_sources + video_node_sources)

    for by_id in sources:
        opened, opened_source = can_open_v4l2(by_id)
        if not opened or opened_source is None:
            continue
        cameras.append((by_id, opened_source))
        if len(cameras) >= limit:
            break
    return cameras


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


def _camera_paths_from_indices(indices: List[int], limit: int) -> List[Tuple[str, int]]:
    cameras: List[Tuple[str, int]] = []
    seen: set[int] = set()
    for idx in indices:
        if idx in seen:
            continue
        if limit >= 0 and len(cameras) >= limit:
            break
        seen.add(idx)
        path = f"/dev/video{idx}"
        if os.path.exists(path):
            opened, opened_source = can_open_v4l2(path)
            if opened and opened_source is not None and isinstance(opened_source, int):
                cameras.append((path, opened_source))
            else:
                # Keep explicit user selection even if current probe fails.
                cameras.append((path, idx))
                print(
                    f"warning: explicit camera index {idx} ({path}) not opened during probe; "
                    "it will be added to output for manual validation."
                )
        else:
            print(f"warning: explicit camera index {idx} does not exist ({path}); skipping")
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
    camera_sources: List[Tuple[str, object]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for i, (device_path, opened_path) in enumerate(camera_sources):
        if i >= len(base_cameras):
            break
        base = dict(base_cameras[i])
        opened_index: Optional[int]
        if isinstance(opened_path, int):
            opened_index = opened_path
        elif isinstance(opened_path, str):
            opened_index = _video_index_for_path(opened_path)
        else:
            opened_index = None

        if opened_index is None and isinstance(device_path, str):
            opened_index = _video_index_for_path(device_path)

        base.update(
            {
                "device_path": device_path,
                "device_index": opened_index if opened_index is not None else i,
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
    parser.add_argument(
        "--camera-indices",
        nargs="+",
        default=None,
        help="Explicit video indices to force into config (e.g. --camera-indices 0 1 2).",
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
    if explicit_indices:
        cameras = _camera_paths_from_indices(explicit_indices, args.max_cameras)
        if not cameras:
            raise SystemExit("No explicit camera indices resolved.")
    else:
        cameras = detect_cameras(args.max_cameras)
        if not cameras:
            raise SystemExit("No working cameras found via CAP_V4L2 probe.")
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
