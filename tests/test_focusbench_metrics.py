import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from focusfield.bench.metrics.metrics import compute_scene_metric
from focusfield.bench.metrics.metrics import summarize_scene_metrics
from focusfield.bench.metrics.scoring import evaluate_gates


def _write_wav(path: Path, data: np.ndarray, sample_rate: int = 16000) -> None:
    x = np.asarray(data, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    pcm = np.clip(x, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(int(x.shape[1]))
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm16.tobytes())


class FocusBenchMetricTests(unittest.TestCase):
    def test_scene_metric_improves_for_cleaner_candidate(self) -> None:
        sr = 16000
        n = sr * 2
        t = np.arange(n, dtype=np.float32) / float(sr)
        target = 0.4 * np.sin(2.0 * np.pi * 220.0 * t)
        interferer = 0.3 * np.sin(2.0 * np.pi * 540.0 * t + 0.33)
        baseline = target + 0.8 * interferer
        candidate = target + 0.35 * interferer

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref_target = tmp_path / "target.wav"
            ref_interf = tmp_path / "interf.wav"
            baseline_wav = tmp_path / "baseline.wav"
            candidate_wav = tmp_path / "candidate.wav"
            _write_wav(ref_target, target, sr)
            _write_wav(ref_interf, interferer, sr)
            _write_wav(baseline_wav, baseline, sr)
            _write_wav(candidate_wav, candidate, sr)

            scene = {
                "scene_id": "synthetic_scene",
                "target_angle_deg": 0,
                "interferer_angle_deg": 120,
                "target_reference_wav": str(ref_target),
                "interferer_reference_wav": str(ref_interf),
                "reference_text": "hello world from focus field",
                "baseline_text": "hello word from focus",
                "candidate_text": "hello world from focus field",
            }
            metric = compute_scene_metric(scene, baseline_wav, candidate_wav)
            self.assertIsNotNone(metric.si_sdr_delta_db)
            self.assertIsNotNone(metric.sir_delta_db)
            self.assertIsNotNone(metric.stoi_delta)
            self.assertIsNotNone(metric.wer_relative_improvement)
            self.assertGreater(metric.si_sdr_delta_db or 0.0, 0.0)
            self.assertGreater(metric.sir_delta_db or 0.0, 0.0)
            self.assertGreater(metric.stoi_delta or 0.0, 0.0)
            self.assertGreater(metric.wer_relative_improvement or 0.0, 0.0)

            summary = summarize_scene_metrics([metric])
            self.assertGreater(summary["median_si_sdr_delta_db"] or 0.0, 0.0)

    def test_gate_scoring_pass_fail(self) -> None:
        quality = {
            "median_si_sdr_delta_db": 2.4,
            "median_stoi_delta": 0.05,
            "median_wer_relative_improvement": 0.2,
            "median_sir_delta_db": 5.0,
        }
        latency = {"p95_ms": 120.0, "p99_ms": 180.0}
        drops = {"queue_full_audio": 4.0, "capture_underrun_rate": 0.001}
        passed = evaluate_gates(quality, latency, drops)
        self.assertTrue(passed["passed"])

        failed = evaluate_gates(
            quality,
            {"p95_ms": 180.0, "p99_ms": 260.0},
            {"queue_full_audio": 90.0, "capture_underrun_rate": 0.05},
        )
        self.assertFalse(failed["passed"])


if __name__ == "__main__":
    unittest.main()
