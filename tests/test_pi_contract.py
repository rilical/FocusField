import threading
import unittest
from unittest.mock import patch

from focusfield.audio.devices import AudioDeviceInfo
from focusfield.core.config import validate_config
from focusfield.main.run import _validate_runtime_requirements
from focusfield.platform import hardware_probe
from focusfield.vision.cameras import _camera_loop


class _DummyLogger:
    def __init__(self) -> None:
        self.events = []

    def emit(self, level, module, event, payload):  # noqa: ANN001
        self.events.append((level, module, event, payload))


class _DummyBus:
    def publish(self, _topic, _msg):  # noqa: ANN001
        return None


class _FakeClosedCapture:
    def isOpened(self) -> bool:  # noqa: N802
        return False

    def release(self) -> None:
        return None


class PiContractTests(unittest.TestCase):
    def test_config_validation_rejects_strict_mismatch(self) -> None:
        cfg = {
            "runtime": {
                "enable_validation": True,
                "requirements": {
                    "strict": True,
                    "min_cameras": 3,
                    "min_audio_channels": 8,
                },
            },
            "audio": {
                "channels": 2,
                "device_profile": "mic_array_2ch_default",
            },
            "video": {
                "cameras": [{"id": "cam0"}],
            },
        }
        errs = validate_config(cfg)
        self.assertTrue(any("runtime.requirements.min_audio_channels" in e for e in errs))
        self.assertTrue(any("runtime.requirements.min_cameras" in e for e in errs))

    def test_config_validation_rejects_invalid_camera_scope(self) -> None:
        cfg = {
            "runtime": {
                "enable_validation": True,
                "requirements": {
                    "camera_scope": "csi",
                },
            },
        }
        errs = validate_config(cfg)
        self.assertTrue(any("runtime.requirements.camera_scope" in e for e in errs))

    def test_config_validation_rejects_invalid_mvdr_new_keys(self) -> None:
        cfg = {
            "runtime": {"enable_validation": True},
            "audio": {
                "channels": 8,
                "device_profile": "minidsp_uma8_raw_7p1",
                "beamformer": {
                    "mvdr": {
                        "weight_interp_alpha": 1.5,
                        "freq_low_hz": 1000,
                        "freq_high_hz": 800,
                        "speech_freeze_covariance": "yes",
                    }
                },
            },
        }
        errs = validate_config(cfg)
        self.assertTrue(any("weight_interp_alpha" in e for e in errs))
        self.assertTrue(any("freq_high_hz" in e for e in errs))
        self.assertTrue(any("speech_freeze_covariance" in e for e in errs))

    def test_config_validation_rejects_invalid_denoise_and_bench_targets(self) -> None:
        cfg = {
            "runtime": {"enable_validation": True},
            "audio": {
                "channels": 8,
                "device_profile": "minidsp_uma8_raw_7p1",
                "denoise": {
                    "backend": "foobar",
                    "rnnoise": {"model_path": 123},
                    "hybrid": {"postfilter_strength": 2.5},
                },
            },
            "bench": {
                "targets": {
                    "si_sdr_delta_db_min": -1,
                    "latency_p95_ms_max": "bad",
                }
            },
        }
        errs = validate_config(cfg)
        self.assertTrue(any("audio.denoise.backend" in e for e in errs))
        self.assertTrue(any("audio.denoise.rnnoise.model_path" in e for e in errs))
        self.assertTrue(any("audio.denoise.hybrid.postfilter_strength" in e for e in errs))
        self.assertTrue(any("bench.targets.si_sdr_delta_db_min" in e for e in errs))
        self.assertTrue(any("bench.targets.latency_p95_ms_max" in e for e in errs))

    def test_collect_camera_sources_prefers_by_path(self) -> None:
        mapping = {
            "/dev/v4l/by-path/*": ["/dev/v4l/by-path/p1", "/dev/v4l/by-path/p2"],
            "/dev/v4l/by-id/*": ["/dev/v4l/by-id/i1"],
            "/dev/video*": ["/dev/video0", "/dev/video1"],
        }
        real = {
            "/dev/v4l/by-path/p1": "/dev/video0",
            "/dev/v4l/by-path/p2": "/dev/video1",
            "/dev/v4l/by-id/i1": "/dev/video0",
            "/dev/video0": "/dev/video0",
            "/dev/video1": "/dev/video1",
        }

        def _glob(pattern: str):
            return mapping.get(pattern, [])

        def _realpath(path: str) -> str:
            return real.get(path, path)

        with patch("focusfield.platform.hardware_probe.glob.glob", side_effect=_glob):
            with patch("focusfield.platform.hardware_probe.os.path.realpath", side_effect=_realpath):
                sources = hardware_probe.collect_camera_sources("auto")
        self.assertGreaterEqual(len(sources), 2)
        self.assertEqual(sources[0], "/dev/v4l/by-path/p1")
        self.assertEqual(sources[1], "/dev/v4l/by-path/p2")

    def test_collect_camera_sources_usb_scope_filters_non_usb(self) -> None:
        mapping = {
            "/dev/v4l/by-path/*": ["/dev/v4l/by-path/p1", "/dev/v4l/by-path/p2"],
            "/dev/v4l/by-id/*": ["/dev/v4l/by-id/i1"],
            "/dev/video*": ["/dev/video0", "/dev/video11"],
        }
        real = {
            "/dev/v4l/by-path/p1": "/dev/video0",
            "/dev/v4l/by-path/p2": "/dev/video11",
            "/dev/v4l/by-id/i1": "/dev/video0",
            "/dev/video0": "/dev/video0",
            "/dev/video11": "/dev/video11",
        }

        def _glob(pattern: str):
            return mapping.get(pattern, [])

        def _realpath(path: str) -> str:
            return real.get(path, path)

        def _is_usb(path: str):
            return path == "/dev/video0"

        with patch("focusfield.platform.hardware_probe.glob.glob", side_effect=_glob):
            with patch("focusfield.platform.hardware_probe.os.path.realpath", side_effect=_realpath):
                with patch("focusfield.platform.hardware_probe.is_usb_video_node", side_effect=_is_usb):
                    sources = hardware_probe.collect_camera_sources("auto", camera_scope="usb")
        self.assertEqual(sources, ["/dev/v4l/by-path/p1"])

    def test_candidate_sources_strict_filters_non_capture(self) -> None:
        with patch("focusfield.platform.hardware_probe.os.path.realpath", return_value="/dev/video1"):
            with patch("focusfield.platform.hardware_probe.is_capture_node", return_value=False):
                out = hardware_probe.candidate_sources("/dev/v4l/by-path/p1", strict_capture=True)
        self.assertEqual(out, [])

    def test_runtime_requirements_fail_when_usb_cameras_below_target(self) -> None:
        cfg = {
            "runtime": {
                "requirements": {"strict": True, "min_cameras": 3, "min_audio_channels": 8, "camera_scope": "usb"},
            },
            "video": {
                "cameras": [{"id": "cam0"}, {"id": "cam1"}, {"id": "cam2"}],
            },
        }
        logger = _DummyLogger()
        camera_probe = [
            (True, [(0, "CAP_V4L2")], (0, "CAP_V4L2")),
            (False, [(1, "CAP_V4L2")], None),
            (False, [(2, "CAP_V4L2")], None),
        ]

        with patch("focusfield.main.run.try_open_camera_any_backend", side_effect=camera_probe):
            with patch("focusfield.main.run.resolve_input_device_index", return_value=2):
                with patch(
                    "focusfield.main.run.list_input_devices",
                    return_value=[
                        AudioDeviceInfo(
                            index=2,
                            name="miniDSP",
                            hostapi="ALSA",
                            max_input_channels=8,
                            default_samplerate_hz=48000.0,
                        )
                    ],
                ):
                    with self.assertRaisesRegex(RuntimeError, r"camera_scope=usb"):
                        _validate_runtime_requirements(cfg, logger)

    def test_runtime_requirements_fail_audio_hint_for_uma8_dsp_mode(self) -> None:
        cfg = {
            "runtime": {
                "requirements": {"strict": True, "min_cameras": 0, "min_audio_channels": 8, "camera_scope": "usb"},
            },
            "video": {
                "cameras": [],
            },
        }
        logger = _DummyLogger()
        with patch("focusfield.main.run.resolve_input_device_index", return_value=2):
            with patch(
                "focusfield.main.run.list_input_devices",
                return_value=[
                    AudioDeviceInfo(
                        index=2,
                        name="miniDSP VocalFusion Spk",
                        hostapi="ALSA",
                        max_input_channels=2,
                        default_samplerate_hz=48000.0,
                    )
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, r"UMA-8 appears in 2ch DSP mode"):
                    _validate_runtime_requirements(cfg, logger)

    def test_runtime_requirements_pass_when_targets_met(self) -> None:
        cfg = {
            "runtime": {
                "requirements": {"strict": True, "min_cameras": 3, "min_audio_channels": 8, "camera_scope": "usb"},
            },
            "video": {
                "cameras": [{"id": "cam0"}, {"id": "cam1"}, {"id": "cam2"}],
            },
        }
        logger = _DummyLogger()
        camera_probe = [
            (True, [(0, "CAP_V4L2")], (0, "CAP_V4L2")),
            (True, [(1, "CAP_V4L2")], (1, "CAP_V4L2")),
            (True, [(2, "CAP_V4L2")], (2, "CAP_V4L2")),
        ]
        with patch("focusfield.main.run.try_open_camera_any_backend", side_effect=camera_probe):
            with patch("focusfield.main.run.resolve_input_device_index", return_value=2):
                with patch(
                    "focusfield.main.run.list_input_devices",
                    return_value=[
                        AudioDeviceInfo(
                            index=2,
                            name="miniDSP UMA-8 RAW",
                            hostapi="ALSA",
                            max_input_channels=8,
                            default_samplerate_hz=48000.0,
                        )
                    ],
                ):
                    _validate_runtime_requirements(cfg, logger)
        self.assertTrue(any(event == "runtime_requirements_passed" for _, _, event, _ in logger.events))

    def test_camera_missing_sets_stop_event_in_fail_fast(self) -> None:
        stop_event = threading.Event()
        logger = _DummyLogger()
        with patch("focusfield.vision.cameras._open_camera", return_value=_FakeClosedCapture()):
            _camera_loop(
                _DummyBus(),
                logger,
                stop_event,
                True,
                "cam0",
                "/dev/video0",
                0,
                640,
                360,
                15,
                "vision.frames.cam0",
            )
        self.assertTrue(stop_event.is_set())
        self.assertTrue(any(event == "camera_missing" for _, _, event, _ in logger.events))


if __name__ == "__main__":
    unittest.main()
