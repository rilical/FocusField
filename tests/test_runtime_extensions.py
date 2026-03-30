import multiprocessing as mp
import queue
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from focusfield.audio.enhance.denoise import _RnnoiseNativeState, _rnnoise_native_denoise, start_denoise
from focusfield.core.bus import Bus
from focusfield.core.config import load_config, validate_config
from focusfield.core.logging import LogEmitter
import focusfield.main.runtime_multiprocess as runtime_multiprocess
import focusfield.main.runtime_support as runtime_support
from focusfield.main.runtime_multiprocess import _decode_payload, _encode_payload  # noqa: PLC2701
from focusfield.main.runtime_support import apply_runtime_os_tuning
from focusfield.vision.mouth.mouth_activity import TFLiteMouthEstimator, _ensure_task_model_path, _ensure_tflite_model_path  # noqa: PLC2701
from focusfield.vision.tracking.face_track import CameraTracker, _ensure_yunet_model, _visual_state_from_features  # noqa: PLC2701
from focusfield.vision.tracking.track_smoothing import TrackSmoother


class RuntimeExtensionsTests(unittest.TestCase):
    def test_validate_config_rejects_invalid_runtime_and_mouth_backends(self) -> None:
        cfg = {
            "runtime": {
                "process_mode": "threadz",
                "realtime": {"scheduler": "deadline", "cpu_affinity": ["x"]},
                "multiprocess": {"start_method": "bad", "queue_depth": 0},
            },
            "vision": {"mouth": {"backend": "foo", "tflite_threads": 0}},
        }
        errs = validate_config(cfg)
        self.assertTrue(any("runtime.process_mode" in e for e in errs))
        self.assertTrue(any("runtime.realtime.scheduler" in e for e in errs))
        self.assertTrue(any("runtime.realtime.cpu_affinity" in e for e in errs))
        self.assertTrue(any("runtime.multiprocess.start_method" in e for e in errs))
        self.assertTrue(any("runtime.multiprocess.queue_depth" in e for e in errs))
        self.assertTrue(any("vision.mouth.backend" in e for e in errs))
        self.assertTrue(any("vision.mouth.tflite_threads" in e for e in errs))

    def test_validate_config_rejects_invalid_bus_queue_policies(self) -> None:
        cfg = {
            "runtime": {"enable_validation": True},
            "bus": {
                "topic_queue_policies": {
                    "audio.frames": "latest",
                    "vision.frames.*": 123,
                }
            },
        }
        errs = validate_config(cfg)
        self.assertTrue(any("bus.topic_queue_policies.audio.frames" in e for e in errs))
        self.assertTrue(any("bus.topic_queue_policies.vision.frames.*" in e for e in errs))

    def test_mode_example_configs_load_with_expected_modes(self) -> None:
        cases = [
            ("configs/meeting_peripheral.yaml", "meeting_peripheral", "usb_mic"),
            ("configs/mac_loopback_dev.yaml", "mac_loopback_dev", "host_loopback"),
            ("configs/appliance_fastboot.yaml", "appliance_fastboot", "usb_mic"),
            ("configs/bench.yaml", "bench", "file"),
        ]
        for path, expected_mode, expected_sink in cases:
            cfg = load_config(path)
            self.assertEqual(cfg["runtime"]["mode"], expected_mode)
            self.assertEqual(cfg["output"]["sink"], expected_sink)

    def test_tflite_model_is_extracted_from_task_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            task_path = tmp_path / "face_landmarker.task"
            with zipfile.ZipFile(task_path, "w") as zf:
                zf.writestr("face_landmarks_detector.tflite", b"fake-model")
            model_path = _ensure_tflite_model_path(
                model_path=None,
                task_path=str(task_path),
                member_name="face_landmarks_detector.tflite",
            )
            self.assertTrue(Path(model_path).exists())
            self.assertEqual(Path(model_path).read_bytes(), b"fake-model")

    def test_runtime_download_guards_fail_fast_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch.dict("os.environ", {"FOCUSFIELD_ALLOW_RUNTIME_DOWNLOADS": "0"}, clear=False):
                with self.assertRaises(RuntimeError):
                    _ensure_task_model_path(str(tmp_path / "missing.task"))
                with self.assertRaises(RuntimeError):
                    _ensure_yunet_model(str(tmp_path / "missing.onnx"))

    def test_visual_state_blends_face_motion_and_presence(self) -> None:
        speechy = _visual_state_from_features(
            mouth_activity=0.15,
            motion_activity=0.85,
            landmark_presence=0.95,
            edge_quality=1.0,
            motion_weight=0.35,
            quality_floor=0.2,
            backend="tflite",
        )
        weak = _visual_state_from_features(
            mouth_activity=0.05,
            motion_activity=0.05,
            landmark_presence=0.0,
            edge_quality=0.6,
            motion_weight=0.35,
            quality_floor=0.2,
            backend="diff",
        )
        self.assertGreater(float(speechy["visual_speaking_prob"]), float(weak["visual_speaking_prob"]))
        self.assertGreater(float(speechy["visual_quality"]), float(weak["visual_quality"]))

    def test_tflite_is_attempted_even_when_facemesh_is_disabled(self) -> None:
        tracker = CameraTracker.__new__(CameraTracker)
        tracker._mouth_backend = "auto"
        tracker._logger = MagicMock()
        tracker._camera_id = "cam0"
        tracker._tflite = None
        tracker._mesh = None

        tflite_stub = MagicMock()
        facemesh_stub = MagicMock()

        with (
            patch("focusfield.vision.tracking.face_track.TFLiteMouthEstimator", return_value=tflite_stub) as tflite_ctor,
            patch("focusfield.vision.tracking.face_track.FaceMeshMouthEstimator", return_value=facemesh_stub) as facemesh_ctor,
        ):
            CameraTracker._init_mouth_model(tracker, {"use_facemesh": False})

        tflite_ctor.assert_called_once()
        facemesh_ctor.assert_not_called()
        self.assertIs(tracker._tflite, tflite_stub)
        self.assertIsNone(tracker._mesh)

    def test_track_smoother_keeps_track_id_through_lateral_motion(self) -> None:
        smoother = TrackSmoother(iou_threshold=0.3, center_gate_px=180.0, velocity_alpha=0.45)
        first = smoother.update([((10, 10, 40, 40), 0.9)])
        self.assertEqual(first[0].track_id, 1)
        second = smoother.update([((60, 10, 40, 40), 0.9)])
        matched = [track for track in second if track.matched]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].track_id, 1)

    def test_rnnoise_onnx_backend_falls_back_when_model_missing(self) -> None:
        config = {
            "audio": {
                "sample_rate_hz": 48000,
                "denoise": {
                    "enabled": True,
                    "backend": "rnnoise_onnx",
                    "rnnoise": {
                        "allow_fallback": True,
                        "model_path": "",
                        "model_url": "",
                        "frame_size": 480,
                    },
                },
            }
        }
        bus = Bus(max_queue_depth=8)
        logger = LogEmitter(bus, min_level="error", run_id="denoise-test")
        stop_event = threading.Event()
        try:
            thread = start_denoise(bus, config, logger, stop_event)
            self.assertIsNotNone(thread)
            q_final = bus.subscribe("audio.enhanced.final")
            frame = (0.01 * np.random.randn(960)).astype(np.float32)
            bus.publish(
                "audio.enhanced.beamformed",
                {
                    "t_ns": 1,
                    "seq": 1,
                    "sample_rate_hz": 48000,
                    "frame_samples": frame.shape[0],
                    "channels": 1,
                    "data": frame,
                },
            )
            msg = q_final.get(timeout=1.0)
            self.assertEqual(int(msg.get("frame_samples", 0)), frame.shape[0])
            self.assertEqual(np.asarray(msg["data"]).shape[0], frame.shape[0])
        finally:
            stop_event.set()
            time.sleep(0.05)

    def test_shared_memory_codec_round_trip(self) -> None:
        payload = {
            "audio": np.arange(12, dtype=np.float32).reshape(3, 4),
            "nested": [{"fft": np.arange(6, dtype=np.complex64)}],
            "tuple": ("x", np.arange(2, dtype=np.int16)),
        }
        encoded = _encode_payload(payload)
        decoded = _decode_payload(encoded)
        self.assertTrue(np.array_equal(decoded["audio"], payload["audio"]))
        self.assertTrue(np.array_equal(decoded["nested"][0]["fft"], payload["nested"][0]["fft"]))
        self.assertEqual(decoded["tuple"][0], "x")
        self.assertTrue(np.array_equal(decoded["tuple"][1], payload["tuple"][1]))

    def test_multiprocess_queue_policy_preserves_latency_for_new_audio(self) -> None:
        q_oldest = queue.Queue(maxsize=1)
        q_oldest.put("stale")
        runtime_multiprocess._put_queue_with_policy(q_oldest, "fresh", "drop_oldest")  # noqa: PLC2701
        self.assertEqual(q_oldest.get_nowait(), "fresh")

        q_newest = queue.Queue(maxsize=1)
        q_newest.put("stale")
        runtime_multiprocess._put_queue_with_policy(q_newest, "fresh", "drop_newest")  # noqa: PLC2701
        self.assertEqual(q_newest.get_nowait(), "stale")

    def test_topic_queue_policy_honors_exact_and_wildcard_rules(self) -> None:
        config = {
            "bus": {
                "topic_queue_policies": {
                    "audio.frames": "drop_oldest",
                    "audio.*": "drop_newest",
                }
            }
        }
        self.assertEqual(runtime_multiprocess._topic_queue_policy(config, "audio.frames"), "drop_oldest")  # noqa: PLC2701
        self.assertEqual(runtime_multiprocess._topic_queue_policy(config, "audio.enhanced.final"), "drop_newest")  # noqa: PLC2701

    def test_tflite_mouth_backend_initializes_when_runtime_is_available(self) -> None:
        try:
            estimator = TFLiteMouthEstimator()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        img = np.zeros((256, 256, 3), dtype=np.uint8)
        value = estimator.estimate_activity(img, (64, 64, 128, 128))
        if value is not None:
            self.assertGreaterEqual(float(value), 0.0)
            self.assertLessEqual(float(value), 1.0)

    def test_rnnoise_native_path_processes_frame_when_available(self) -> None:
        state = _RnnoiseNativeState(sample_rate_hz=48000)

        class _Logger:
            def emit(self, *_args, **_kwargs) -> None:
                return None

        frame = (0.01 * np.random.randn(960)).astype(np.float32)
        out = _rnnoise_native_denoise(frame, state, _Logger(), "rnnoise_native", 48000)
        if out is None:
            self.skipTest("pyrnnoise is not available")
        self.assertEqual(out.shape, frame.shape)
        self.assertTrue(np.isfinite(out).all())

    def test_apply_runtime_os_tuning_invokes_platform_hooks(self) -> None:
        config = {
            "runtime": {
                "realtime": {
                    "enabled": True,
                    "allow_best_effort": False,
                    "cpu_affinity": [3, 1, 3],
                    "scheduler": "fifo",
                    "priority": 12,
                    "mlockall": True,
                    "nice": -5,
                }
            }
        }
        logger = MagicMock()
        fake_libc = MagicMock()
        fake_libc.mlockall.return_value = 0

        with (
            patch.object(runtime_support.os, "sched_setaffinity", create=True) as sched_setaffinity,
            patch.object(runtime_support.os, "sched_setscheduler", create=True) as sched_setscheduler,
            patch.object(runtime_support.os, "sched_param", side_effect=lambda value: value, create=True),
            patch.object(runtime_support.os, "SCHED_FIFO", 7, create=True),
            patch.object(runtime_support.os, "nice", create=True) as nice_call,
            patch.object(runtime_support.ctypes, "CDLL", return_value=fake_libc),
        ):
            apply_runtime_os_tuning(config, logger, role="main")

        sched_setaffinity.assert_called_once_with(0, [1, 3])
        sched_setscheduler.assert_called_once_with(0, 7, 12)
        nice_call.assert_called_once_with(-5)
        fake_libc.mlockall.assert_called_once()
        logger.emit.assert_called()

    @unittest.skipUnless("fork" in mp.get_all_start_methods(), "requires fork start method")
    def test_multiprocess_runtime_bridges_messages_both_directions(self) -> None:
        config = {
            "logging": {"level": "error"},
            "runtime": {
                "process_mode": "multiprocess",
                "multiprocess": {
                    "start_method": "fork",
                    "queue_depth": 8,
                    "shared_memory": True,
                },
            },
            "video": {"cameras": [{"id": "cam0"}]},
        }
        bus = Bus(max_queue_depth=16)
        logger = LogEmitter(bus, min_level="error", run_id="runtime-mp-test")
        stop_event = threading.Event()
        q_audio = bus.subscribe("audio.enhanced.final")
        q_vision = bus.subscribe("vision.face_tracks")
        q_debug = bus.subscribe("audio.beamformer.debug")

        def _fake_audio_worker(local_bus, _config, _logger, local_stop_event):
            q_lock = local_bus.subscribe("fusion.target_lock")

            def _run() -> None:
                local_bus.publish(
                    "audio.enhanced.final",
                    {
                        "t_ns": 1,
                        "seq": 1,
                        "sample_rate_hz": 16000,
                        "frame_samples": 4,
                        "channels": 1,
                        "data": np.arange(4, dtype=np.float32),
                    },
                )
                while not local_stop_event.is_set():
                    try:
                        msg = q_lock.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    local_bus.publish(
                        "audio.beamformer.debug",
                        {
                            "t_ns": int(msg.get("t_ns", 0)),
                            "echo_state": msg.get("state"),
                            "echo_bearing_deg": msg.get("target_bearing_deg"),
                        },
                    )
                    return

            thread = threading.Thread(target=_run, name="fake-audio-worker", daemon=True)
            thread.start()
            return [thread]

        def _fake_vision_worker(local_bus, _config, _logger, local_stop_event, _req):
            def _run() -> None:
                local_bus.publish(
                    "vision.face_tracks",
                    {
                        "t_ns": 2,
                        "tracks": [{"track_id": "speaker-1", "camera_id": "cam0"}],
                    },
                )
                while not local_stop_event.is_set():
                    time.sleep(0.01)

            thread = threading.Thread(target=_run, name="fake-vision-worker", daemon=True)
            thread.start()
            return [thread]

        try:
            with (
                patch.object(runtime_multiprocess, "_start_audio_worker", side_effect=_fake_audio_worker),
                patch.object(runtime_multiprocess, "_start_vision_worker", side_effect=_fake_vision_worker),
            ):
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message="This process .* use of fork\\(\\) may lead to deadlocks in the child\\.",
                        category=DeprecationWarning,
                    )
                    runtime_multiprocess.start_multiprocess_runtime(bus, config, logger, stop_event)
                audio_msg = q_audio.get(timeout=3.0)
                vision_msg = q_vision.get(timeout=3.0)
                bus.publish("fusion.target_lock", {"t_ns": 33, "state": "LOCKED", "target_bearing_deg": 15.0})
                debug_msg = q_debug.get(timeout=3.0)
        finally:
            stop_event.set()
            time.sleep(0.3)

        self.assertTrue(np.array_equal(np.asarray(audio_msg["data"]), np.arange(4, dtype=np.float32)))
        self.assertEqual(vision_msg["tracks"][0]["track_id"], "speaker-1")
        self.assertEqual(debug_msg["echo_state"], "LOCKED")
        self.assertEqual(float(debug_msg["echo_bearing_deg"]), 15.0)


if __name__ == "__main__":
    unittest.main()
