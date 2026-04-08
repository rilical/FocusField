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
_USB_TOPOLOGY_TOKEN = re.compile(r"^\d+-\d+(\.\d+)*(:\d+\.\d+)?$")


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


def is_usb_video_node(path: str) -> bool | None:
    index = video_index_for_source(path)
    if index is None:
        return None

    device_link = Path(f"/sys/class/video4linux/video{index}/device")
    if not device_link.exists():
        return None

    try:
        resolved = device_link.resolve()
    except Exception:
        return None

    for part in resolved.parts:
        if part.startswith("usb") or _USB_TOPOLOGY_TOKEN.match(part):
            return True
    return False


def normalize_camera_scope(camera_scope: str | None) -> str:
    scope = str(camera_scope or "any").strip().lower()
    if scope not in {"any", "usb"}:
        raise ValueError(f"Unsupported camera scope: {camera_scope}")
    return scope


def source_matches_camera_scope(source: object, camera_scope: str = "any") -> bool:
    scope = normalize_camera_scope(camera_scope)
    if scope == "any":
        return True

    candidate: str | None = None
    if isinstance(source, int):
        candidate = f"/dev/video{source}"
    elif isinstance(source, str):
        if source.startswith("/dev/video"):
            candidate = source
        else:
            try:
                resolved = os.path.realpath(source)
            except Exception:
                resolved = source
            if isinstance(resolved, str) and resolved.startswith("/dev/video"):
                candidate = resolved

    if candidate is None:
        return False
    return is_usb_video_node(candidate) is True


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


def _normalize_camera_group_key(source: str) -> tuple[str, int]:
    name = os.path.basename(source)
    match = re.search(r"-video-index(\d+)$", name)
    if match is None:
        return source, 0
    idx = int(match.group(1))
    root = name[: match.start()]
    root = root.replace("-usbv2", "-usb")
    return root, idx


def _coalesce_multi_interface_sources(sources: list[str]) -> list[str]:
    groups: dict[str, list[tuple[int, str]]] = {}
    for source in sources:
        key, idx = _normalize_camera_group_key(source)
        groups.setdefault(key, []).append((idx, source))

    kept: list[str] = []
    for key in sorted(groups):
        candidates = groups[key]
        candidates.sort(key=lambda item: item[0])
        kept.append(candidates[0][1])
    return kept


def collect_camera_sources(camera_source: str = "auto", camera_scope: str = "any") -> list[str]:
    source_mode = str(camera_source or "auto").strip().lower()
    scope_mode = normalize_camera_scope(camera_scope)
    by_path = sorted(glob.glob("/dev/v4l/by-path/*"))
    by_id = sorted(glob.glob("/dev/v4l/by-id/*"))
    nodes = video_nodes()

    if source_mode == "by-path":
        by_path = _coalesce_multi_interface_sources(by_path)
        return _filter_sources_by_scope(_dedupe_paths(by_path), scope_mode)
    if source_mode == "by-id":
        by_id = _coalesce_multi_interface_sources(by_id)
        return _filter_sources_by_scope(_dedupe_paths(by_id), scope_mode)
    if source_mode == "index":
        return _filter_sources_by_scope(_dedupe_paths(nodes), scope_mode)
    if source_mode != "auto":
        raise ValueError(f"Unsupported camera source mode: {camera_source}")

    # Auto mode: prefer by-path for stable physical port identity, then by-id, then raw nodes.
    return _filter_sources_by_scope(_dedupe_paths(by_path + by_id + nodes), scope_mode)


def _filter_sources_by_scope(sources: list[str], camera_scope: str) -> list[str]:
    if camera_scope == "any":
        return sources
    filtered: list[str] = []
    for source in sources:
        if source_matches_camera_scope(source, camera_scope=camera_scope):
            filtered.append(source)
    return filtered


def candidate_sources(source: object, strict_capture: bool = False, camera_scope: str = "any") -> list[object]:
    scope_mode = normalize_camera_scope(camera_scope)
    if not isinstance(source, str):
        return [source] if source_matches_camera_scope(source, scope_mode) else []

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
            capture = is_capture_node(resolved)
            if capture is not False:
                if resolved not in sources:
                    sources.append(resolved)
                m = re.search(r"/dev/video(\d+)$", resolved)
                if m is not None:
                    video_source = f"/dev/video{m.group(1)}"
                    if video_source not in sources:
                        sources.append(video_source)
            elif not strict_capture:
                if resolved not in sources:
                    sources.append(resolved)
                m = re.search(r"/dev/video(\d+)$", resolved)
                if m is not None:
                    video_source = f"/dev/video{m.group(1)}"
                    if video_source not in sources:
                        sources.append(video_source)
            return _finalize_candidates(sources, strict_capture, scope_mode)

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

    return _finalize_candidates(sources, strict_capture, scope_mode)


def _finalize_candidates(values: list[object], strict_capture: bool, camera_scope: str) -> list[object]:
    deduped: list[object] = []
    for value in values:
        if value in deduped:
            continue
        if not source_matches_camera_scope(value, camera_scope):
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
    # OpenCV on the Pi can treat integer sources as camera indices rather than
    # concrete V4L2 device nodes. Preserve explicit /dev/videoN paths so
    # CAP_V4L2 opens the actual node instead of probing "camera 0/1/2".
    return source


def try_open_camera_any_backend(
    source: object,
    strict_capture: bool = False,
    camera_scope: str = "any",
) -> tuple[bool, list[tuple[object, str]], tuple[object, str] | None]:
    if cv2 is None:
        return False, [], None

    if strict_capture:
        backends = [("CAP_V4L2", cv2.CAP_V4L2)]
    else:
        backends = [
            ("CAP_V4L2", cv2.CAP_V4L2),
            ("CAP_ANY", cv2.CAP_ANY),
        ]
    tried: list[tuple[object, str]] = []
    for candidate in candidate_sources(source, strict_capture=strict_capture, camera_scope=camera_scope):
        source_for_open = source_to_open_target(candidate)
        for backend_name, backend in backends:
            tried.append((candidate, backend_name))
            cap = cv2.VideoCapture(source_for_open, backend)
            if cap.isOpened():
                cap.release()
                return True, tried, (candidate, backend_name)
            cap.release()
    return False, tried, None
