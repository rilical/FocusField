import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from focusfield.audio.devices import AudioDeviceInfo, resolve_input_device_index
from focusfield.main.run import _runtime_base_dir  # noqa: PLC2701
from focusfield.ui.telemetry import _build_snapshot, _merge_camera_map  # noqa: PLC2701
from focusfield.vision.calibration.runtime_overlay import (
    apply_audio_calibration_sidecar,
    apply_camera_calibration_sidecar,
)


class CameraCalibrationRuntimeTests(unittest.TestCase):
    def _sidecar_path(self, tmpdir: str) -> Path:
        return Path(tmpdir) / "camera_calibration.json"

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

    def test_camera_calibration_sidecar_detects_duplicates_and_invalid_yaw(self) -> None:
        cfg = {
            "video": {
                "cameras": [
                    {"id": "cam0", "yaw_offset_deg": 0.0},
                ]
            },
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            self._sidecar_path(tmpdir).write_text(
                json.dumps(
                    {
                        "cameras": [
                            {"id": "cam0", "yaw_offset_deg": "NaN"},
                            {"id": "cam0", "yaw_offset_deg": 725.0},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            overlay = apply_camera_calibration_sidecar(cfg, base_dir=Path(tmpdir))
        cameras = {str(cam.get("id")): cam for cam in cfg["video"]["cameras"]}
        self.assertAlmostEqual(float(cameras["cam0"]["yaw_offset_deg"]), 5.0)
        self.assertIn("cam0", overlay["modified_camera_ids"])
        self.assertIn("cam0", overlay.get("duplicate_camera_ids", []))
        self.assertTrue(any(issue.get("code") == "yaw_invalid" for issue in overlay.get("validation", {}).get("errors", [])))
        self.assertTrue(any(issue.get("code") == "yaw_normalized" for issue in overlay.get("validation", {}).get("warnings", [])))

    def test_camera_calibration_sidecar_tracks_stale_and_missing_configured_cameras(self) -> None:
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
            self._sidecar_path(tmpdir).write_text(json.dumps({"cameras": [{"id": "cam0", "yaw_offset_deg": 12.0}]}), encoding="utf-8")
            overlay = apply_camera_calibration_sidecar(cfg, base_dir=Path(tmpdir))
        camera_states = overlay.get("camera_states", [])
        self.assertEqual(overlay["status"], "stale")
        self.assertEqual(set(overlay.get("missing_camera_ids", [])), {"cam1", "cam2"})
        self.assertTrue(any(state.get("camera_id") == "cam1" and state.get("status") == "stale" for state in camera_states))
        self.assertTrue(any(state.get("camera_id") == "cam2" and state.get("status") == "stale" for state in camera_states))
        self.assertEqual(len([s for s in camera_states if s.get("status") == "active"]), 1)

    def test_camera_calibration_sidecar_normalizes_out_of_range_yaw(self) -> None:
        cfg = {
            "video": {"cameras": [{"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 160.0}]},
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            self._sidecar_path(tmpdir).write_text(json.dumps({"cameras": [{"id": "cam0", "yaw_offset_deg": -45.0}]}), encoding="utf-8")
            overlay = apply_camera_calibration_sidecar(cfg, base_dir=Path(tmpdir))
        cameras = {str(cam.get("id")): cam for cam in cfg["video"]["cameras"]}
        self.assertAlmostEqual(float(cameras["cam0"]["yaw_offset_deg"]), 315.0)
        self.assertIn("yaw_normalized", {issue.get("code") for issue in overlay.get("validation", {}).get("warnings", [])})
        self.assertIn("cam0", overlay["applied_camera_ids"])

    def test_camera_calibration_sidecar_preserves_bearing_fields_and_resolves_relative_lut_path(self) -> None:
        cfg = {
            "video": {
                "cameras": [
                    {
                        "id": "cam0",
                        "yaw_offset_deg": 0.0,
                        "bearing_model": "linear",
                        "bearing_offset_deg": 1.5,
                        "bearing_lut_path": "",
                    }
                ]
            },
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "calibration"
            base_dir.mkdir(parents=True, exist_ok=True)
            (base_dir / "lut.json").write_text(json.dumps([0.0, 10.0, 20.0]), encoding="utf-8")
            self._sidecar_path(str(base_dir)).write_text(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "id": "cam0",
                                "yaw_offset_deg": 22.0,
                                "bearing_model": "lut",
                                "bearing_offset_deg": -3.25,
                                "bearing_lut_path": "lut.json",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            overlay = apply_camera_calibration_sidecar(cfg, base_dir=base_dir)
        camera = cfg["video"]["cameras"][0]
        expected_lut_path = str((base_dir / "lut.json").resolve())
        self.assertEqual(camera["bearing_model"], "lut")
        self.assertAlmostEqual(float(camera["bearing_offset_deg"]), -3.25)
        self.assertEqual(camera["bearing_lut_path"], expected_lut_path)
        self.assertEqual(overlay["cameras"][0]["bearing_lut_path"], expected_lut_path)
        self.assertIn("cam0", overlay["modified_camera_ids"])

    def test_audio_calibration_sidecar_applies_startup_offset_and_reports_restart_truth(self) -> None:
        cfg = {
            "audio": {
                "yaw_offset_deg": 12.5,
            },
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "audio_calibration.json").write_text(
                json.dumps({"yaw_offset_deg": -2.0}),
                encoding="utf-8",
            )
            overlay = apply_audio_calibration_sidecar(cfg, base_dir=base_dir)
        self.assertAlmostEqual(float(cfg["audio"]["yaw_offset_deg"]), 10.5)
        self.assertTrue(bool(overlay["active"]))
        self.assertEqual(overlay["status"], "active")
        self.assertAlmostEqual(float(overlay["base_runtime_yaw_offset_deg"]), 12.5)
        self.assertAlmostEqual(float(overlay["sidecar_yaw_offset_deg"]), -2.0)
        self.assertAlmostEqual(float(overlay["effective_runtime_yaw_offset_deg"]), 10.5)
        self.assertTrue(bool(overlay["restart_required_on_change"]))
        self.assertFalse(bool(overlay["hot_reload_supported"]))
        self.assertEqual(str(overlay["reload_behavior"]), "startup_only")

    def test_runtime_base_dir_prefers_effective_config_env_before_runtime_metadata(self) -> None:
        cfg = {
            "runtime": {
                "config_path": "/tmp/invoked/config.yaml",
            }
        }
        with patch.dict(
            os.environ,
            {
                "FOCUSFIELD_CONFIG_EFFECTIVE": "/tmp/generated/effective.yaml",
                "FOCUSFIELD_CONFIG_PATH": "/tmp/invoked/override.yaml",
            },
            clear=False,
        ):
            resolved = _runtime_base_dir(cfg)
        self.assertEqual(resolved, Path("/tmp/generated").resolve())

    def test_camera_calibration_hfov_suspicious_assumptions_are_exposed(self) -> None:
        cfg = {
            "video": {
                "cameras": [
                    {"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 0.0},
                ]
            },
            "runtime": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            self._sidecar_path(tmpdir).write_text(json.dumps({"cameras": [{"id": "cam0", "yaw_offset_deg": 0.0}]}), encoding="utf-8")
            overlay = apply_camera_calibration_sidecar(cfg, base_dir=Path(tmpdir))
        self.assertTrue(overlay.get("status") in {"stale", "error"})
        self.assertTrue(
            any(issue.get("code") == "hfov_suspicious_range" for issue in overlay.get("validation", {}).get("warnings", []))
        )

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
                "audio_calibration_overlay": {
                    "active": True,
                    "status": "active",
                    "reload_behavior": "startup_only",
                    "hot_reload_supported": False,
                    "restart_required_on_change": True,
                },
                "audio_yaw_calibration": {
                    "profile_yaw_offset_deg": 15.0,
                    "base_runtime_yaw_offset_deg": 12.0,
                    "sidecar_yaw_offset_deg": -2.0,
                    "effective_total_yaw_offset_deg": 25.0,
                },
            },
            "configured_cameras": ["cam0"],
            "configured_camera_map": _merge_camera_map(
                [
                    {
                        "id": "cam0",
                        "yaw_offset_deg": 0.0,
                        "hfov_deg": 160.0,
                        "bearing_model": "linear",
                        "bearing_offset_deg": 0.0,
                        "bearing_lut_path": "",
                    }
                ],
                [
                    {
                        "id": "cam0",
                        "yaw_offset_deg": 33.0,
                        "bearing_model": "lut",
                        "bearing_offset_deg": 4.0,
                        "bearing_lut_path": "/tmp/lut.json",
                    }
                ],
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
        self.assertEqual(snapshot["meta"]["camera_map"][0]["bearing_model"], "lut")
        self.assertAlmostEqual(float(snapshot["meta"]["camera_map"][0]["bearing_offset_deg"]), 4.0)
        self.assertEqual(snapshot["meta"]["audio_device"]["device_index"], 4)
        self.assertEqual(snapshot["meta"]["audio_calibration_overlay"]["reload_behavior"], "startup_only")
        self.assertAlmostEqual(float(snapshot["meta"]["runtime_config"]["audio_yaw_calibration"]["effective_total_yaw_offset_deg"]), 25.0)
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

    def test_resolve_input_device_index_ignores_stale_explicit_index(self) -> None:
        devices = [
            AudioDeviceInfo(
                index=2,
                name="micArray RAW SPK: USB Audio",
                hostapi="ALSA",
                max_input_channels=8,
                default_samplerate_hz=48000.0,
            ),
            AudioDeviceInfo(
                index=4,
                name="Arducam 1080P Low Light: USB Audio",
                hostapi="ALSA",
                max_input_channels=2,
                default_samplerate_hz=44100.0,
            ),
        ]
        config = {
            "audio": {
                "channels": 8,
                "device_index": 4,
                "device_selector": {
                    "match_substring": "micArray RAW SPK",
                    "require_input_channels": 8,
                },
            }
        }
        with patch("focusfield.audio.devices.list_input_devices", return_value=devices):
            idx = resolve_input_device_index(config)
        self.assertEqual(idx, 2)

    def test_resolve_input_device_index_avoids_loopback_input_for_host_loopback_output(self) -> None:
        devices = [
            AudioDeviceInfo(
                index=1,
                name="BlackHole 2ch",
                hostapi="Core Audio",
                max_input_channels=2,
                default_samplerate_hz=48000.0,
            ),
            AudioDeviceInfo(
                index=2,
                name="External Microphone",
                hostapi="Core Audio",
                max_input_channels=1,
                default_samplerate_hz=48000.0,
            ),
            AudioDeviceInfo(
                index=4,
                name="MacBook Pro Microphone",
                hostapi="Core Audio",
                max_input_channels=1,
                default_samplerate_hz=48000.0,
            ),
        ]
        config = {
            "audio": {"channels": 1},
            "output": {
                "sink": "host_loopback",
                "host_loopback": {"device_selector": {"match_substring": "BlackHole"}},
            },
        }
        with patch("focusfield.audio.devices.list_input_devices", return_value=devices):
            idx = resolve_input_device_index(config)
        self.assertEqual(idx, 2)


if __name__ == "__main__":
    unittest.main()
