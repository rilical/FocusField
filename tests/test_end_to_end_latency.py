import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from focusfield.audio.beamform.delay_and_sum import start_delay_and_sum
from focusfield.audio.beamform.mvdr import start_mvdr
from focusfield.audio.enhance.denoise import start_denoise
from focusfield.core.bus import Bus
from focusfield.core.clock import now_ns
from focusfield.core.logging import LogEmitter


class EndToEndLatencyTests(unittest.TestCase):
    def test_audio_pipeline_end_to_end_latency_logged_to_txt(self) -> None:
        sample_rate = 16000
        frame_samples = 512
        channels = 4
        frames_to_measure = 8
        log_path = Path("artifacts") / "tests" / "e2e_latency_ms.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        config = {
            "audio": {
                "sample_rate_hz": sample_rate,
                "block_size": frame_samples,
                "channels": channels,
                "beamformer": {
                    "method": "delay_and_sum",
                    "no_lock_behavior": "omni",
                },
                "denoise": {
                    "enabled": True,
                    "backend": "wiener",
                    "wiener": {"nfft": 512},
                },
            }
        }

        bus = Bus(max_queue_depth=16)
        logger = LogEmitter(bus, min_level="error", run_id="latency-test")
        stop_event = threading.Event()

        try:
            with patch(
                "focusfield.audio.beamform.delay_and_sum.load_mic_positions",
                return_value=([
                    (0.0, 0.0),
                    (0.04, 0.0),
                    (0.0, 0.04),
                    (0.04, 0.04),
                ], [0, 1, 2, 3]),
            ):
                beam_thread = start_delay_and_sum(bus, config, logger, stop_event)
            denoise_thread = start_denoise(bus, config, logger, stop_event)

            self.assertIsNotNone(beam_thread)
            self.assertIsNotNone(denoise_thread)

            q_final = bus.subscribe("audio.enhanced.final")
            latencies_ms = []

            for seq in range(1, frames_to_measure + 1):
                frame = (0.05 * np.random.randn(frame_samples, channels)).astype(np.float32)
                t_in_ns = now_ns()
                bus.publish(
                    "audio.frames",
                    {
                        "t_ns": t_in_ns,
                        "seq": seq,
                        "sample_rate_hz": sample_rate,
                        "frame_samples": frame_samples,
                        "channels": channels,
                        "data": frame,
                    },
                )

                msg_out = q_final.get(timeout=1.0)
                t_out_ns = now_ns()
                latency_ms = (t_out_ns - int(msg_out.get("t_ns", t_in_ns))) / 1_000_000.0
                latencies_ms.append(latency_ms)

            mean_ms = float(np.mean(latencies_ms))
            p95_ms = float(np.percentile(np.asarray(latencies_ms, dtype=np.float64), 95.0))

            lines = [
                f"samples={len(latencies_ms)}",
                f"mean_ms={mean_ms:.3f}",
                f"p95_ms={p95_ms:.3f}",
            ]
            lines.extend(f"latency_ms[{idx}]={value:.3f}" for idx, value in enumerate(latencies_ms, start=1))
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("mean_ms=", text)
            self.assertIn("p95_ms=", text)
            self.assertGreater(mean_ms, 0.0)
            self.assertLess(mean_ms, 500.0)
        finally:
            stop_event.set()
            time.sleep(0.05)

    def test_mvdr_pipeline_end_to_end_latency_logged_to_txt(self) -> None:
        sample_rate = 16000
        frame_samples = 512
        channels = 8
        frames_to_measure = 8
        log_path = Path("artifacts") / "tests" / "e2e_latency_mvdr_ms.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        angles = np.linspace(0.0, 2.0 * np.pi, num=channels, endpoint=False)
        positions = [(0.05 * float(np.cos(angle)), 0.05 * float(np.sin(angle))) for angle in angles]

        config = {
            "audio": {
                "sample_rate_hz": sample_rate,
                "block_size": frame_samples,
                "channels": channels,
                "beamformer": {
                    "method": "mvdr",
                    "no_lock_behavior": "omni",
                    "use_last_lock_ms": 500.0,
                    "mvdr": {
                        "nfft": 512,
                        "refresh_ms": 15.0,
                        "noise_ema_alpha": 0.95,
                        "diagonal_loading": 1e-3,
                        "steering_update_deg": 5.0,
                        "max_condition_number": 1e6,
                    },
                },
                "denoise": {
                    "enabled": True,
                    "backend": "wiener",
                    "wiener": {"nfft": 512},
                },
            }
        }

        bus = Bus(max_queue_depth=16)
        logger = LogEmitter(bus, min_level="error", run_id="latency-test-mvdr")
        stop_event = threading.Event()

        try:
            with patch(
                "focusfield.audio.beamform.mvdr.load_mic_positions",
                return_value=(positions, list(range(channels))),
            ):
                beam_thread = start_mvdr(bus, config, logger, stop_event)
            denoise_thread = start_denoise(bus, config, logger, stop_event)

            self.assertIsNotNone(beam_thread)
            self.assertIsNotNone(denoise_thread)

            q_final = bus.subscribe("audio.enhanced.final")
            bus.publish(
                "fusion.target_lock",
                {
                    "t_ns": now_ns(),
                    "state": "LOCKED",
                    "target_bearing_deg": 20.0,
                },
            )
            bus.publish("audio.vad", {"t_ns": now_ns(), "speech": True, "confidence": 0.95})

            latencies_ms = []
            for seq in range(1, frames_to_measure + 1):
                frame = (0.05 * np.random.randn(frame_samples, channels)).astype(np.float32)
                t_in_ns = now_ns()
                bus.publish(
                    "audio.frames",
                    {
                        "t_ns": t_in_ns,
                        "seq": seq,
                        "sample_rate_hz": sample_rate,
                        "frame_samples": frame_samples,
                        "channels": channels,
                        "data": frame,
                    },
                )

                msg_out = q_final.get(timeout=1.0)
                t_out_ns = now_ns()
                latency_ms = (t_out_ns - int(msg_out.get("t_ns", t_in_ns))) / 1_000_000.0
                latencies_ms.append(latency_ms)

            mean_ms = float(np.mean(latencies_ms))
            p95_ms = float(np.percentile(np.asarray(latencies_ms, dtype=np.float64), 95.0))

            lines = [
                f"samples={len(latencies_ms)}",
                f"mean_ms={mean_ms:.3f}",
                f"p95_ms={p95_ms:.3f}",
            ]
            lines.extend(f"latency_ms[{idx}]={value:.3f}" for idx, value in enumerate(latencies_ms, start=1))
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("mean_ms=", text)
            self.assertIn("p95_ms=", text)
            self.assertGreater(mean_ms, 0.0)
            self.assertLess(mean_ms, 500.0)
        finally:
            stop_event.set()
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
