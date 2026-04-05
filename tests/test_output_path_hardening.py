import threading
import time
import unittest
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np

from focusfield.audio.enhance.agc_post import AdaptiveGainLimiter
from focusfield.audio.output import sink as output_sink
from focusfield.audio.output import virtual_mic
from focusfield.core.bus import Bus
from focusfield.core.logging import LogEmitter
from focusfield.core.perf_monitor import start_perf_monitor


class OutputPathHardeningTests(unittest.TestCase):
    def test_agc_limiter_soft_limits_loud_frame(self) -> None:
        agc = AdaptiveGainLimiter(
            enabled=True,
            target_rms=0.1,
            max_gain=4.0,
            min_gain=0.4,
            attack_alpha=0.0,
            release_alpha=0.0,
            limiter_threshold=0.5,
        )
        frame = np.ones(512, dtype=np.float32) * 1.0
        out, stats = agc.process(frame)
        self.assertEqual(out.shape, frame.shape)
        self.assertLess(float(np.max(np.abs(out))), 1.0)
        self.assertGreaterEqual(float(stats["clipped"]), 0.0)
        self.assertLessEqual(float(stats["peak"]), 0.5)

    def test_output_sink_routes_usb_mic_and_host_loopback(self) -> None:
        bus = Bus(max_queue_depth=4)
        logger = MagicMock()
        stop_event = threading.Event()

        with patch.object(output_sink, "start_usb_mic_sink", return_value="usb-thread") as usb_sink:
            self.assertEqual(
                output_sink.start_output_sink(bus, {"output": {"sink": "usb_mic"}}, logger, stop_event),
                "usb-thread",
            )
            usb_sink.assert_called_once()

        with patch.object(output_sink, "start_host_loopback_sink", return_value="loopback-thread") as host_sink:
            self.assertEqual(
                output_sink.start_output_sink(bus, {"output": {"sink": "host_loopback"}}, logger, stop_event),
                "loopback-thread",
            )
            host_sink.assert_called_once()

    def test_output_device_selector_exact_name_takes_precedence_over_substring(self) -> None:
        config = {
            "output": {
                "usb_mic": {
                    "device_selector": {
                        "exact_name": "FocusField USB Mic",
                        "match_substring": "USB",
                    }
                }
            }
        }
        devices = [
            virtual_mic.AudioOutputDeviceInfo(
                index=1,
                name="Generic USB Speaker",
                hostapi="CoreAudio",
                max_output_channels=8,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=2,
                name="FocusField USB Mic",
                hostapi="CoreAudio",
                max_output_channels=2,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=3,
                name="FocusField USB Mic",
                hostapi="CoreAudio",
                max_output_channels=4,
                default_samplerate_hz=48_000.0,
            ),
        ]

        with patch.object(virtual_mic, "list_output_devices", return_value=devices):
            chosen = virtual_mic.resolve_output_device_index(config, section_name="usb_mic")

        self.assertEqual(chosen, 3)

    def test_output_device_selector_hostapi_filters_exact_name_candidates(self) -> None:
        config = {
            "output": {
                "usb_mic": {
                    "device_selector": {
                        "exact_name": "FocusField USB Mic",
                        "hostapi": "CoreAudio",
                    }
                }
            }
        }
        devices = [
            virtual_mic.AudioOutputDeviceInfo(
                index=4,
                name="FocusField USB Mic",
                hostapi="WASAPI",
                max_output_channels=8,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=5,
                name="FocusField USB Mic",
                hostapi="CoreAudio",
                max_output_channels=2,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=6,
                name="FocusField USB Mic",
                hostapi="CoreAudio",
                max_output_channels=4,
                default_samplerate_hz=48_000.0,
            ),
        ]

        with patch.object(virtual_mic, "list_output_devices", return_value=devices):
            chosen = virtual_mic.resolve_output_device_index(config, section_name="usb_mic")

        self.assertEqual(chosen, 6)

    def test_output_device_selector_hostapi_filters_substring_candidates(self) -> None:
        config = {
            "output": {
                "usb_mic": {
                    "device_selector": {
                        "match_substring": "USB",
                        "hostapi": "WASAPI",
                    }
                }
            }
        }
        devices = [
            virtual_mic.AudioOutputDeviceInfo(
                index=7,
                name="USB Output A",
                hostapi="CoreAudio",
                max_output_channels=8,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=8,
                name="USB Output B",
                hostapi="WASAPI",
                max_output_channels=2,
                default_samplerate_hz=48_000.0,
            ),
            virtual_mic.AudioOutputDeviceInfo(
                index=9,
                name="USB Output C",
                hostapi="WASAPI",
                max_output_channels=6,
                default_samplerate_hz=48_000.0,
            ),
        ]

        with patch.object(virtual_mic, "list_output_devices", return_value=devices):
            chosen = virtual_mic.resolve_output_device_index(config, section_name="usb_mic")

        self.assertEqual(chosen, 9)

    def test_usb_mic_sink_retries_when_exact_name_selector_finds_no_device(self) -> None:
        bus = Bus(max_queue_depth=4)
        logger = MagicMock()
        stop_event = threading.Event()
        config = {
            "audio": {
                "sample_rate_hz": 48_000,
                "block_size": 256,
            },
            "output": {
                "usb_mic": {
                    "channels": 1,
                    "reconnect_delay_ms": 100,
                    "device_selector": {
                        "exact_name": "FocusField USB Mic",
                        "hostapi": "ALSA",
                    },
                }
            },
        }
        fake_sd = MagicMock()
        fake_sd.OutputStream.side_effect = AssertionError("should not open default output stream")

        with (
            patch.object(virtual_mic, "sd", fake_sd),
            patch.object(virtual_mic, "list_output_devices", return_value=[]),
        ):
            thread = virtual_mic.start_usb_mic_sink(bus, config, logger, stop_event)
            self.assertIsNotNone(thread)
            time.sleep(0.15)
            stop_event.set()
            time.sleep(0.05)

        fake_sd.OutputStream.assert_not_called()
        logger.emit.assert_any_call(
            "warning",
            "audio.output.usb_mic",
            "device_not_found",
            {
                "exact_name": "FocusField USB Mic",
                "hostapi": "ALSA",
                "retry_in_ms": 100,
            },
        )

    def test_perf_monitor_includes_output_stage_metrics(self) -> None:
        bus = Bus(max_queue_depth=8)
        logger = LogEmitter(bus, min_level="error", run_id="perf-output-test")
        stop_event = threading.Event()
        config = {
            "perf": {"enabled": True, "emit_hz": 10.0},
            "runtime": {"artifacts": {"dir_run": ""}},
        }
        thread = start_perf_monitor(bus, config, logger, stop_event)
        self.assertIsNotNone(thread)
        q_perf = bus.subscribe("runtime.perf")

        try:
            bus.publish(
                "audio.frames",
                {
                    "t_ns": 1000,
                    "seq": 1,
                    "sample_rate_hz": 48000,
                    "frame_samples": 256,
                    "channels": 1,
                    "data": np.zeros(256, dtype=np.float32),
                },
            )
            bus.publish(
                "audio.enhanced.final",
                {
                    "t_ns": 4000,
                    "seq": 1,
                    "sample_rate_hz": 48000,
                    "frame_samples": 256,
                    "channels": 1,
                    "data": np.zeros(256, dtype=np.float32),
                    "stage_timestamps": {
                        "captured_t_ns": 1000,
                        "beamformed_t_ns": 2000,
                        "denoised_t_ns": 3000,
                        "published_t_ns": 4000,
                    },
                },
            )
            bus.publish(
                "audio.output.stats",
                {
                    "t_ns": 6000,
                    "sink": "usb_mic",
                    "backend": "sounddevice",
                    "device_name": "FocusField",
                    "occupancy_frames": 128,
                    "target_buffer_frames": 256,
                    "buffer_capacity_frames": 1024,
                    "resample_ratio": 1.0,
                    "input_age_ms": 2.5,
                    "stage_timestamps": {
                        "captured_t_ns": 1000,
                        "beamformed_t_ns": 2000,
                        "denoised_t_ns": 3000,
                        "published_t_ns": 4000,
                    },
                    "underrun_total": 1,
                    "overrun_total": 2,
                    "device_error_total": 0,
                    "sample_rate_mismatch_total": 0,
                    "block_size_mismatch_total": 0,
                },
            )

            perf = None
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    perf = q_perf.get(timeout=0.25)
                    if isinstance(perf, dict) and perf.get("audio_output"):
                        break
                except Exception:
                    continue

            self.assertIsInstance(perf, dict)
            self.assertIn("audio_output", perf)
            self.assertEqual(perf["audio_output"]["sink"], "usb_mic")
            self.assertEqual(perf["audio_output"]["underrun_total"], 1)
            self.assertIn("stage_latency_ms", perf)
            self.assertIn("stage_latency_rolling_ms", perf)
            self.assertIn("shed_state", perf)
            self.assertGreaterEqual(int(perf["shed_state"]["level"]), 0)
            self.assertAlmostEqual(float(perf["stage_latency_ms"]["capture_to_publish_ms"]), 3.0 / 1000.0, places=6)
            self.assertAlmostEqual(float(perf["stage_latency_ms"]["capture_to_output_ms"]), 5.0 / 1000.0, places=6)
            self.assertAlmostEqual(float(perf["stage_latency_ms"]["capture_to_denoise_ms"]), 2.0 / 1000.0, places=6)
        finally:
            stop_event.set()
            time.sleep(0.1)

    def test_file_sink_emits_worker_heartbeat_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = Bus(max_queue_depth=4)
            logger = LogEmitter(bus, min_level="error", run_id="file-sink-test")
            stop_event = threading.Event()
            config = {
                "output": {"sink": "file", "file_sink": {"dir": tmpdir}},
                "trace": {"enabled": False},
                "runtime": {"artifacts": {"dir_run": ""}},
            }
            thread = output_sink.start_output_sink(bus, config, logger, stop_event)
            self.assertIsNotNone(thread)
            q_worker = bus.subscribe("runtime.worker_loop")
            try:
                heartbeat = None
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    try:
                        heartbeat = q_worker.get(timeout=0.25)
                        if isinstance(heartbeat, dict) and heartbeat.get("module") == "audio.output.file_sink":
                            break
                    except Exception:
                        continue
                self.assertIsInstance(heartbeat, dict)
                self.assertEqual(heartbeat.get("module"), "audio.output.file_sink")
                self.assertIn("processed_cycles", heartbeat)
            finally:
                stop_event.set()
                time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
