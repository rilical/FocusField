#!/usr/bin/env python3
"""Boot/install validation helpers for FocusField startup scripts."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from focusfield.core.config import get_path, load_config
from focusfield.main.modes import normalize_runtime_mode


FAST_BOOT_MODES = {"meeting_peripheral", "appliance_fastboot"}


def _service_user_home() -> Path:
    service_user = str(os.environ.get("FOCUSFIELD_SERVICE_USER", "") or "").strip()
    if service_user:
        try:
            return Path(pwd.getpwnam(service_user).pw_dir)
        except KeyError:
            pass
    sudo_user = str(os.environ.get("SUDO_USER", "") or "").strip()
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_model_cache() -> Path:
    return _service_user_home() / ".cache" / "focusfield"


def default_yunet_model() -> Path:
    return default_model_cache() / "face_detection_yunet_2023mar.onnx"


def default_face_landmarker_task() -> Path:
    return default_model_cache() / "face_landmarker.task"


def load_effective_config(config_path: str) -> Dict[str, Any]:
    return load_config(config_path)


def boot_plan(config: Dict[str, Any]) -> Dict[str, Any]:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    startup_cfg = runtime_cfg.get("startup", {})
    if not isinstance(startup_cfg, dict):
        startup_cfg = {}
    mode = normalize_runtime_mode(runtime_cfg.get("mode", "mac_loopback_dev"))
    audio_only = mode in FAST_BOOT_MODES
    return {
        "mode": mode,
        "audio_only": audio_only,
        "preflight_strict": not audio_only,
        "require_cameras": 0 if audio_only else int(get_path(config, "runtime.requirements.min_cameras", 0) or 0),
        "require_audio_channels": 1 if audio_only else int(get_path(config, "runtime.requirements.min_audio_channels", 0) or 0),
        "camera_source": "auto" if audio_only else "by-path",
        "camera_scope": "any" if audio_only else str(get_path(config, "runtime.requirements.camera_scope", "usb") or "usb"),
        "validate_runtime_models": bool(startup_cfg.get("validate_runtime_models", False)),
    }


def validate_local_model_assets(config: Dict[str, Any], config_path: str) -> List[str]:
    errors: List[str] = []
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    mode = normalize_runtime_mode(runtime_cfg.get("mode", "mac_loopback_dev"))
    allow_downloads = bool(get_path(config, "vision.models.allow_runtime_downloads", True)) and bool(
        get_path(config, "audio.models.allow_runtime_downloads", True)
    )
    if allow_downloads and mode not in FAST_BOOT_MODES:
        return errors

    config_root = Path(config_path).expanduser().resolve().parent

    def _resolve(path_value: Any) -> Path:
        path = Path(str(path_value or "")).expanduser()
        if not path.is_absolute():
            path = (config_root / path).resolve()
        return path

    def _require_file(path_value: Any, label: str) -> None:
        if not path_value:
            errors.append(f"{label} must point to a bundled local asset when runtime downloads are disabled")
            return
        path = _resolve(path_value)
        if not path.exists():
            errors.append(f"{label} missing: {path}")

    face_cfg = get_path(config, "vision.face", {})
    if not isinstance(face_cfg, dict):
        face_cfg = {}
    mouth_cfg = get_path(config, "vision.mouth", {})
    if not isinstance(mouth_cfg, dict):
        mouth_cfg = {}
    vad_cfg = get_path(config, "audio.vad", {})
    if not isinstance(vad_cfg, dict):
        vad_cfg = {}
    denoise_cfg = get_path(config, "audio.denoise", {})
    if not isinstance(denoise_cfg, dict):
        denoise_cfg = {}
    rnnoise_cfg = denoise_cfg.get("rnnoise", {})
    if not isinstance(rnnoise_cfg, dict):
        rnnoise_cfg = {}

    face_backend = str(face_cfg.get("backend", "auto") or "auto").strip().lower()
    if face_backend in {"auto", "yunet"}:
        yunet_model = face_cfg.get("yunet_model_path", "") or str(default_yunet_model())
        _require_file(yunet_model, "vision.face.yunet_model_path")

    mouth_backend = str(mouth_cfg.get("backend", "auto") or "auto").strip().lower()
    use_facemesh = bool(mouth_cfg.get("use_facemesh", True))
    if use_facemesh or mouth_backend in {"tflite", "facemesh"}:
        tflite_model_path = mouth_cfg.get("tflite_model_path", "")
        mesh_model_path = mouth_cfg.get("mesh_model_path", "") or str(default_face_landmarker_task())
        if tflite_model_path:
            _require_file(tflite_model_path, "vision.mouth.tflite_model_path")
        elif mesh_model_path:
            mesh_path = _resolve(mesh_model_path)
            if not mesh_path.exists():
                errors.append(f"vision.mouth.mesh_model_path missing: {mesh_path}")
            elif not zipfile.is_zipfile(mesh_path):
                errors.append(f"vision.mouth.mesh_model_path must reference a task bundle: {mesh_path}")
            else:
                with zipfile.ZipFile(mesh_path) as zf:
                    if "face_landmarks_detector.tflite" not in zf.namelist():
                        errors.append(
                            "vision.mouth.mesh_model_path must contain face_landmarks_detector.tflite"
                        )
        else:
            errors.append(
                "vision.mouth.tflite_model_path or vision.mouth.mesh_model_path must be set when runtime downloads are disabled"
            )

    if str(vad_cfg.get("backend", "auto") or "auto").strip().lower() == "silero":
        _require_file(vad_cfg.get("silero", {}).get("model_path", ""), "audio.vad.silero.model_path")

    rnnoise_backend = str(denoise_cfg.get("backend", "wiener") or "wiener").strip().lower()
    if rnnoise_backend in {"rnnoise", "rnnoise_onnx", "hybrid"}:
        model_path = rnnoise_cfg.get("model_path", "")
        model_url = rnnoise_cfg.get("model_url", "")
        if model_path:
            _require_file(model_path, "audio.denoise.rnnoise.model_path")
        elif model_url:
            errors.append(
                "audio.denoise.rnnoise.model_url requires runtime downloads; set audio.denoise.rnnoise.model_path to a local asset"
            )
        else:
            errors.append(
                "audio.denoise.rnnoise.model_path must be set when runtime downloads are disabled"
            )

    return errors


def _format_shell_var(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FocusField boot validation helper")
    parser.add_argument("--config", required=True, help="Path to the effective config file")
    parser.add_argument("--emit-shell-vars", action="store_true", help="Emit shell variables for boot scripts")
    parser.add_argument("--validate-local-models", action="store_true", help="Validate bundled model assets")
    args = parser.parse_args(argv)

    config = load_effective_config(args.config)
    if args.emit_shell_vars:
        plan = boot_plan(config)
        for key, value in plan.items():
            print(f"FOCUSFIELD_BOOT_{key.upper()}={_format_shell_var(value)}")
    if args.validate_local_models:
        errors = validate_local_model_assets(config, args.config)
        if errors:
            for error in errors:
                print(f"boot_validation_error: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
