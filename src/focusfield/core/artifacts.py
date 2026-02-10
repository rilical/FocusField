"""focusfield.core.artifacts

CONTRACT: docs/11_contract_index.md
ROLE: Create per-run artifact directories and write run metadata.

OUTPUTS:
  - artifacts/<run_id>/... folders
  - run_meta.json
  - config_effective.yaml

CONFIG KEYS:
  - runtime.run_id: optional explicit run id
  - runtime.artifacts.dir: base artifacts directory
  - runtime.artifacts.retention.max_runs: keep last N runs
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from focusfield.core.clock import now_ns


def create_run_dir(base_dir: str, run_id: Optional[str] = None, max_runs: int = 10) -> Path:
    """Create and return the run artifact directory.

    Directory structure is fixed to support easy debugging and tooling.
    """

    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    run_id_final = (run_id or "").strip() or _default_run_id()
    run_path = base_path / run_id_final
    if run_path.exists():
        suffix = 2
        while (base_path / f"{run_id_final}_{suffix:02d}").exists():
            suffix += 1
        run_path = base_path / f"{run_id_final}_{suffix:02d}"

    (run_path / "logs").mkdir(parents=True, exist_ok=True)
    (run_path / "crash").mkdir(parents=True, exist_ok=True)
    (run_path / "audio").mkdir(parents=True, exist_ok=True)
    (run_path / "traces").mkdir(parents=True, exist_ok=True)
    (run_path / "thumbs").mkdir(parents=True, exist_ok=True)

    apply_retention(base_path, max_runs=max_runs, keep_dir=run_path)
    return run_path


def apply_retention(base_dir: Path, max_runs: int, keep_dir: Optional[Path] = None) -> None:
    if max_runs <= 0:
        return
    keep_resolved = keep_dir.resolve() if keep_dir is not None else None
    run_dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name != "LATEST"]
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in run_dirs[max_runs:]:
        try:
            if keep_resolved is not None and old.resolve() == keep_resolved:
                continue
            shutil.rmtree(old, ignore_errors=True)
        except Exception:  # noqa: BLE001
            continue


def write_run_metadata(run_dir: Path, config: Dict[str, Any]) -> None:
    """Write run_meta.json and config_effective.yaml."""

    meta = {
        "t_start_ns": now_ns(),
        "platform": {
            "python": sys.version,
            "machine": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
        },
        "versions": _versions(),
        "git": {
            "commit": _git_commit(),
        },
        "devices": {
            "audio": _audio_device_meta(config),
            "cameras": _camera_meta(config),
        },
        "config": config,
    }
    run_meta_path = run_dir / "run_meta.json"
    with open(run_meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)

    cfg_path = run_dir / "config_effective.yaml"
    with open(cfg_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False)

    # Also write a stable, small pointer file for tooling.
    latest_path = run_dir.parent / "LATEST"
    try:
        latest_path.write_text(str(run_dir.name), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _default_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_commit() -> Optional[str]:
    try:
        root = _repo_root()
        out = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode("utf-8").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _versions() -> Dict[str, Optional[str]]:
    versions: Dict[str, Optional[str]] = {}
    try:
        from focusfield.version import __version__

        versions["focusfield"] = str(__version__)
    except Exception:  # noqa: BLE001
        versions["focusfield"] = None
    try:
        import numpy

        versions["numpy"] = str(numpy.__version__)
    except Exception:  # noqa: BLE001
        versions["numpy"] = None
    try:
        import cv2

        versions["opencv"] = str(getattr(cv2, "__version__", None))
    except Exception:  # noqa: BLE001
        versions["opencv"] = None
    try:
        import sounddevice

        versions["sounddevice"] = str(getattr(sounddevice, "__version__", None))
    except Exception:  # noqa: BLE001
        versions["sounddevice"] = None
    return versions


def _audio_device_meta(config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from focusfield.audio.devices import list_input_devices, resolve_input_device_index

        idx = resolve_input_device_index(config)
        devices = list_input_devices()
        dev = next((d for d in devices if d.index == idx), None)
        if dev is None:
            return {"resolved_device_index": idx, "resolved_device": None}
        return {
            "resolved_device_index": int(dev.index),
            "resolved_device": {
                "name": dev.name,
                "hostapi": dev.hostapi,
                "max_input_channels": dev.max_input_channels,
                "default_samplerate_hz": dev.default_samplerate_hz,
            },
        }
    except Exception:  # noqa: BLE001
        return {"resolved_device_index": None, "resolved_device": None}


def _camera_meta(config: Dict[str, Any]) -> Any:
    cameras = config.get("video", {}).get("cameras", [])
    if not isinstance(cameras, list):
        return []
    out = []
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        out.append(
            {
                "id": cam.get("id"),
                "device_path": cam.get("device_path"),
                "device_index": cam.get("device_index"),
                "width": cam.get("width"),
                "height": cam.get("height"),
                "fps": cam.get("fps"),
                "hfov_deg": cam.get("hfov_deg"),
                "yaw_offset_deg": cam.get("yaw_offset_deg"),
            }
        )
    return out
