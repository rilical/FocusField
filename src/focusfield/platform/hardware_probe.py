"""Hardware probing helpers for Pi camera/audio bring-up."""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Optional

try:
    import cv2
except Exception:  # pragma: no cover - runtime platform dependency
    cv2 = None


_V4L2_CAPTURE_BITS = (0x00000001, 0x00001000)


def video_index_for_source(path: str) -> Optional[int]:
    match = re.search(r"/dev/video(\d+)$", path)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def is_capture_node(path: str) -> bool | None:
    index = video_index_for_source(path)
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


def video_nodes() -> list[str]:
    return sorted(path for path in glob.glob("/dev/video*") if re.match(r"^/dev/video\d+$", path))


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        try:
            resolved = os.path.realpath(path)
        except Exception:
            resolved = path
        key = resolved or path
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def collect_camera_sources(camera_source: str = "auto") -> list[str]:
    source_mode = str(camera_source or "auto").strip().lower()
    by_path = sorted(glob.glob("/dev/v4l/by-path/*"))
    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    nodes = video_nodes()

    if source_mode == "by-path":
        return _dedupe_paths(by_path)
    if source_mode == "by-id":
        return _dedupe_paths(by_id)
    if source_mode == "index":
        return _dedupe_paths(nodes)
    if source_mode != "auto":
        raise ValueError(f"Unsupported camera source mode: {camera_source}")

    # Auto mode: prefer by-path for stable physical port identity, then by-id, then raw nodes.
    return _dedupe_paths(by_path + by_id + nodes)


def candidate_sources(source: object, strict_capture: bool = False) -> list[object]:
    if not isinstance(source, str):
        return [source]

    sources: list[object] = []
    source_is_video = source.startswith("/dev/video")
    source_is_v4l_link = source.startswith("/dev/v4l/")

    if source_is_video:
        sources.append(source)

    resolved: str | None = None
    try:
        resolved = os.path.realpath(source)
    except Exception:
        resolved = None

    if resolved and resolved != source:
        if resolved.startswith("/dev/video"):
            if not strict_capture or is_capture_node(resolved) is not False:
                if resolved not in sources:
                    sources.append(resolved)
                m = re.search(r"/dev/video(\d+)$", resolved)
                if m is not None:
                    video_source = f"/dev/video{m.group(1)}"
                    if video_source not in sources:
                        sources.append(video_source)
            return _finalize_candidates(sources, strict_capture)

        if resolved not in sources:
            sources.append(resolved)

    if not source_is_v4l_link or resolved is None:
        if source not in sources:
            sources.append(source)
    if not source_is_video and source not in sources:
        sources.append(source)

    if source_is_video and not strict_capture and is_capture_node(source) is False:
        source_index = video_index_for_source(source)
        if source_index is not None and source_index not in sources:
            sources.append(source_index)

    return _finalize_candidates(sources, strict_capture)


def _finalize_candidates(values: list[object], strict_capture: bool) -> list[object]:
    deduped: list[object] = []
    for value in values:
        if value in deduped:
            continue
        if not strict_capture:
            deduped.append(value)
            continue

        if isinstance(value, int):
            capture = is_capture_node(f"/dev/video{value}")
            if capture is False:
                continue
            deduped.append(value)
            continue

        if isinstance(value, str):
            match = re.search(r"/dev/video\d+$", value)
            if match is not None:
                capture = is_capture_node(value)
                if capture is False:
                    continue
            deduped.append(value)
            continue

        deduped.append(value)
    return deduped


def source_to_open_target(source: object) -> object:
    if not isinstance(source, str):
        return source
    match = re.search(r"/dev/video(\d+)$", source)
    if match is None:
        return source
    try:
        return int(match.group(1))
    except Exception:
        return source


def try_open_camera_any_backend(
    source: object,
    strict_capture: bool = False,
) -> tuple[bool, list[tuple[object, str]], tuple[object, str] | None]:
    if cv2 is None:
        return False, [], None

    backends = [
        ("CAP_V4L2", cv2.CAP_V4L2),
        ("CAP_ANY", cv2.CAP_ANY),
    ]
    tried: list[tuple[object, str]] = []
    for candidate in candidate_sources(source, strict_capture=strict_capture):
        source_for_open = source_to_open_target(candidate)
        for backend_name, backend in backends:
            tried.append((candidate, backend_name))
            cap = cv2.VideoCapture(source_for_open, backend)
            if cap.isOpened():
                cap.release()
                return True, tried, (candidate, backend_name)
            cap.release()
    return False, tried, None

