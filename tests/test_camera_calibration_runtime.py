import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from focusfield.audio.devices import AudioDeviceInfo, resolve_input_device_index
from focusfield.ui.telemetry import _build_snapshot, _merge_camera_map  # noqa: PLC2701
from focusfield.vision.calibration.runtime_overlay import apply_camera_calibration_sidecar


class CameraCalibrationRuntimeTests(unittest.TestCase):
    def test_camera_calibration_sidecar_updates_runtime_config(self) -> None:
        cfg = {
            "video": {
                "cameras": [
                    {"id": "cam0", "yaw_offset_deg": 0.0},
                    {"id": "cam1", "yaw_offset_deg": 120.0},
                    {"id": "cam2", "yaw_offset_deg": 240.0},
                ]
            },
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path("camera_calibration.json").write_text(
                    json.dumps({"cameras": [{"id": "cam1", "yaw_offset_deg": 137.0}]}),
                    encoding="utf-8",
                )
                overlay = apply_camera_calibration_sidecar(cfg)
            finally:
                os.chdir(old_cwd)
        cameras = {str(cam.get("id")): cam for cam in cfg["video"]["cameras"]}
        self.assertAlmostEqual(float(cameras["cam1"]["yaw_offset_deg"]), 137.0)
        self.assertTrue(bool(overlay["active"]))
        self.assertIn("cam1", overlay["modified_camera_ids"])

    def test_telemetry_snapshot_surfaces_camera_map_and_mic_health_summary(self) -> None:
        state = {
            "audio_heatmap": {},
            "heatmap": {},
            "lock": {},
            "faces": [],
            "logs": [],
            "beam": {"fallback_active": True},
            "mic_health": {
                "channels": [
                    {"channel": 0, "score": 0.0, "trust": 0.1, "bad_reason": "dead,dropout"},
                    {"channel": 1, "score": 0.8, "trust": 0.9, "bad_reason": ""},
                ],
                "active_channels": [1],
                "mean_score": 0.4,
                "mean_trust": 0.5,
            },
            "runtime_cfg": {
                "selected_audio_device": {"device_index": 4, "device_name": "micArray RAW SPK", "channels": 8},
                "camera_calibration_overlay": {"active": True},
            },
            "configured_cameras": ["cam0"],
            "configured_camera_map": _merge_camera_map(
                [{"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 160.0}],
                [{"id": "cam0", "yaw_offset_deg": 33.0}],
            ),
            "runtime_profile": "realtime_pi_max",
            "strict_requirements_passed": True,
            "detector_backend_active": "yunet",
            "vision_debug": {},
            "overflow_window": 0,
        }
        snapshot = _build_snapshot(state, 7)
        self.assertEqual(snapshot["mic_health_summary"]["dead_channels"], [0])
        self.assertEqual(snapshot["mic_health_summary"]["active_channels"], [1])
        self.assertEqual(float(snapshot["meta"]["camera_map"][0]["yaw_offset_deg"]), 33.0)
        self.assertEqual(snapshot["meta"]["audio_device"]["device_index"], 4)
        self.assertTrue(snapshot["audio_fallback_active"])

    def test_resolve_input_device_index_prefers_raw_array_device(self) -> None:
        devices = [
            AudioDeviceInfo(
                index=1,
                name="USB Multichannel Capture",
                hostapi="ALSA",
                max_input_channels=8,
                default_samplerate_hz=48000.0,
            ),
            AudioDeviceInfo(
                index=4,
                name="micArray RAW SPK: USB Audio",
                hostapi="ALSA",
                max_input_channels=8,
                default_samplerate_hz=48000.0,
            ),
        ]
        with patch("focusfield.audio.devices.list_input_devices", return_value=devices):
            idx = resolve_input_device_index({"audio": {"channels": 8}})
        self.assertEqual(idx, 4)


if __name__ == "__main__":
    unittest.main()
