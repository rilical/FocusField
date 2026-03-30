import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from focusfield.bench.focusbench import run_focusbench
from focusfield.bench.metrics.metrics import compute_conversation_metrics
from focusfield.bench.metrics.metrics import compute_label_scene_metrics
from focusfield.bench.metrics.metrics import compute_scene_metric
from focusfield.bench.metrics.metrics import compute_runtime_summary
from focusfield.bench.metrics.metrics import summarize_label_scene_metrics
from focusfield.bench.metrics.metrics import summarize_scene_metrics
from focusfield.bench.metrics.scoring import evaluate_gates


def _write_wav(path: Path, data: np.ndarray, sample_rate: int = 16000) -> None:
    x = np.asarray(data, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    pcm = np.clip(x, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(int(x.shape[1]))
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm16.tobytes())


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


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
        lock = {"rms_step_deg": 3.0}
        conversation = {
            "handoff_latency_p95_ms": 250.0,
            "false_handoff_rate": 0.05,
            "no_lock_during_speech_ratio": 0.08,
        }
        runtime = {"queue_pressure_peak": 8.0, "output_underrun_rate": 0.002}
        passed = evaluate_gates(quality, latency, drops, lock_summary=lock, conversation_summary=conversation, runtime_summary=runtime)
        self.assertTrue(passed["passed"])

        failed = evaluate_gates(
            quality,
            {"p95_ms": 180.0, "p99_ms": 260.0},
            {"queue_full_audio": 90.0, "capture_underrun_rate": 0.05},
            lock_summary={"rms_step_deg": 18.0},
            conversation_summary={
                "handoff_latency_p95_ms": 1200.0,
                "false_handoff_rate": 0.35,
                "no_lock_during_speech_ratio": 0.4,
            },
            runtime_summary={"queue_pressure_peak": 80.0, "output_underrun_rate": 0.05},
        )
        self.assertFalse(failed["passed"])

        failed_names = {check["name"] for check in failed["checks"] if not check["passed"]}
        self.assertIn("lock_jitter_rms", failed_names)
        self.assertIn("handoff_latency_p95_ms", failed_names)
        self.assertIn("false_handoff_rate", failed_names)
        self.assertIn("no_lock_during_speech_ratio", failed_names)
        self.assertIn("output_underrun_rate", failed_names)
        self.assertIn("queue_pressure", failed_names)

        permissive = evaluate_gates(quality, latency, drops, lock_summary=lock, conversation_summary=conversation, runtime_summary=runtime, label_summary={})
        self.assertTrue(permissive["passed"])

        strict_missing = evaluate_gates(
            quality,
            latency,
            drops,
            lock_summary=lock,
            conversation_summary=conversation,
            runtime_summary=runtime,
            label_summary={},
            strict_truth=True,
        )
        self.assertFalse(strict_missing["passed"])
        strict_failed_names = {check["name"] for check in strict_missing["checks"] if not check["passed"]}
        self.assertIn("label_supported_scene_count", strict_failed_names)

    def test_runtime_and_conversation_summaries_parse_bench_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            perf = tmp_path / "logs" / "perf.jsonl"
            events = tmp_path / "logs" / "events.jsonl"
            lock = tmp_path / "traces" / "lock.jsonl"
            faces = tmp_path / "traces" / "faces.jsonl"
            _write_jsonl(
                perf,
                [
                    {
                        "t_ns": 1_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 110.0},
                        "queue_pressure": {"drop_total_window": 3, "capture_overflow_window": 1},
                        "output": {
                            "underrun_window": 2,
                            "underrun_total": 2,
                            "overrun_total": 1,
                            "device_error_total": 0,
                            "occupancy_frames": 8,
                            "buffer_capacity_frames": 16,
                        },
                    },
                    {
                        "t_ns": 2_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 120.0},
                        "queue_pressure": {"drop_total_window": 5, "capture_overflow_window": 2},
                        "output": {
                            "underrun_window": 1,
                            "underrun_total": 3,
                            "overrun_total": 2,
                            "device_error_total": 0,
                            "occupancy_frames": 12,
                            "buffer_capacity_frames": 16,
                        },
                    },
                ],
            )
            _write_jsonl(
                events,
                [
                    {
                        "t_ns": 1_000_000_000,
                        "reason": "handoff_start",
                        "target_id": "speaker-a",
                        "context": {
                            "module": "fusion.av_association",
                            "event": "handoff_start",
                            "details": {"target_id": "speaker-a"},
                        }
                    },
                    {
                        "t_ns": 1_250_000_000,
                        "reason": "handoff_commit",
                        "target_id": "speaker-a",
                        "context": {
                            "module": "fusion.av_association",
                            "event": "handoff_commit",
                            "details": {"target_id": "speaker-a"},
                        }
                    },
                    {
                        "t_ns": 1_400_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": True},
                        }
                    },
                    {
                        "t_ns": 1_500_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_600_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_700_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_800_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                ],
            )
            _write_jsonl(
                lock,
                [
                    {"t_ns": 1_000_000_000, "reason": "handoff_start", "target_id": "speaker-a", "state": "HANDOFF", "target_bearing_deg": 0.0},
                    {"t_ns": 1_250_000_000, "reason": "handoff_commit", "target_id": "speaker-a", "state": "LOCKED", "target_bearing_deg": 0.0},
                    {"t_ns": 1_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 0.0},
                    {"t_ns": 1_100_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 2.0},
                    {"t_ns": 1_200_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 3.0},
                ],
            )
            _write_jsonl(faces, [{"t_ns": 1_000_000_000, "faces": [{}]}, {"t_ns": 2_000_000_000, "faces": [{}]}])

            runtime_summary = compute_runtime_summary(perf)
            self.assertGreater(runtime_summary["queue_pressure_peak"] or 0.0, 0.0)
            self.assertGreater(runtime_summary["output_underrun_rate"] or 0.0, 0.0)

            conversation_summary = compute_conversation_metrics(lock, faces, events)
            self.assertIsNotNone(conversation_summary["handoff_latency_p95_ms"])
            self.assertIsNotNone(conversation_summary["no_lock_during_speech_ratio"])
            self.assertIsNotNone(conversation_summary["false_handoff_rate"])

    def test_label_scene_metrics_and_summary_use_recorded_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock = tmp_path / "traces" / "lock.jsonl"
            faces = tmp_path / "traces" / "faces.jsonl"
            _write_jsonl(
                lock,
                [
                    {"t_ns": 1_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 1.0},
                    {"t_ns": 2_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 21.0},
                    {"t_ns": 3_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 20.0},
                ],
            )
            _write_jsonl(
                faces,
                [
                    {"t_ns": 1_000_000_000, "faces": []},
                    {"t_ns": 1_500_000_000, "faces": [{}]},
                    {"t_ns": 2_000_000_000, "faces": []},
                    {"t_ns": 2_250_000_000, "faces": [{}]},
                ],
            )
            scene = {
                "scene_id": "labeled_scene",
                "labels": {
                    "speaker_segments": [
                        {"start_s": 1.0, "end_s": 2.0, "speaker_id": "speaker-a"},
                        {"start_s": 2.0, "end_s": 3.0, "speaker_id": "speaker-a"},
                    ],
                    "bearing_segments": [
                        {"start_s": 1.0, "end_s": 2.0, "bearing_deg": 1.0},
                        {"start_s": 2.0, "end_s": 3.0, "bearing_deg": 21.0},
                    ],
                    "track_segments": [
                        {"start_s": 1.0, "end_s": 2.0, "speaker_id": "speaker-a", "track_id": "track-a"},
                        {"start_s": 2.0, "end_s": 3.0, "speaker_id": "speaker-a", "track_id": "track-a"},
                    ],
                    "face_segments": [
                        {"start_s": 0.0, "end_s": 1.0, "present": False},
                        {"start_s": 1.0, "end_s": 1.5, "present": True},
                        {"start_s": 2.0, "end_s": 2.25, "present": False},
                        {"start_s": 2.25, "end_s": 3.0, "present": True},
                    ],
                },
            }

            metrics = compute_label_scene_metrics(scene, lock, faces, conversation_summary={"face_reacquire_latency_p95_ms": 250.0})
            self.assertAlmostEqual(metrics["speaker_selection_accuracy"] or 0.0, 1.0)
            self.assertIsNotNone(metrics["steering_mae_deg"])
            self.assertLess(metrics["steering_mae_deg"], 2.0)
            self.assertIsNotNone(metrics["id_churn_rate"])
            self.assertLess(metrics["id_churn_rate"], 0.1)
            self.assertIsNotNone(metrics["face_reacquire_latency_p95_ms"])

            summary = summarize_label_scene_metrics([metrics])
            self.assertEqual(summary["label_supported_scene_count"], 1.0)
            self.assertAlmostEqual(summary["speaker_selection_accuracy"] or 0.0, 1.0)
            self.assertIsNotNone(summary["steering_mae_deg"])
            self.assertLess(summary["steering_mae_deg"], 2.0)
            self.assertIsNotNone(summary["id_churn_rate"])
            self.assertLess(summary["id_churn_rate"], 0.1)
            self.assertIsNotNone(summary["face_reacquire_latency_p95_ms"])

    def test_run_focusbench_emits_meeting_grade_gates(self) -> None:
        sr = 16000
        n = sr
        t = np.arange(n, dtype=np.float32) / float(sr)
        target = 0.35 * np.sin(2.0 * np.pi * 220.0 * t)
        interferer = 0.28 * np.sin(2.0 * np.pi * 540.0 * t + 0.33)
        baseline = target + 0.4 * np.sin(2.0 * np.pi * 540.0 * t)
        candidate = target + 0.1 * np.sin(2.0 * np.pi * 540.0 * t)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_run = root / "baseline"
            candidate_run = root / "candidate"
            manifest = root / "scenes.yaml"
            out_dir = root / "out"
            _write_wav(root / "refs" / "target.wav", target, sr)
            _write_wav(root / "refs" / "interferer.wav", interferer, sr)
            _write_wav(baseline_run / "audio" / "enhanced.wav", baseline, sr)
            _write_wav(candidate_run / "audio" / "enhanced.wav", candidate, sr)
            _write_jsonl(
                candidate_run / "logs" / "perf.jsonl",
                [
                    {
                        "t_ns": 1_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 100.0},
                        "queue_pressure": {"drop_total_window": 2},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 4, "buffer_capacity_frames": 16},
                    },
                    {
                        "t_ns": 2_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 105.0},
                        "queue_pressure": {"drop_total_window": 3},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 5, "buffer_capacity_frames": 16},
                    },
                ],
            )
            _write_jsonl(
                candidate_run / "logs" / "events.jsonl",
                [
                    {
                        "t_ns": 1_000_000_000,
                        "reason": "handoff_start",
                        "target_id": "speaker-a",
                        "context": {
                            "module": "fusion.av_association",
                            "event": "handoff_start",
                            "details": {"target_id": "speaker-a"},
                        }
                    },
                    {
                        "t_ns": 1_250_000_000,
                        "reason": "handoff_commit",
                        "target_id": "speaker-a",
                        "context": {
                            "module": "fusion.av_association",
                            "event": "handoff_commit",
                            "details": {"target_id": "speaker-a"},
                        }
                    },
                    {
                        "t_ns": 1_400_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": True},
                        }
                    },
                    {
                        "t_ns": 1_500_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_600_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_700_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                    {
                        "t_ns": 1_800_000_000,
                        "context": {
                            "module": "fusion.av_association",
                            "event": "no_candidates",
                            "details": {"vad_speech": False},
                        }
                    },
                ],
            )
            _write_jsonl(
                candidate_run / "traces" / "lock.jsonl",
                [
                    {"t_ns": 1_000_000_000, "reason": "handoff_start", "target_id": "speaker-a", "state": "HANDOFF", "target_bearing_deg": 0.0},
                    {"t_ns": 1_250_000_000, "reason": "handoff_commit", "target_id": "speaker-a", "state": "LOCKED", "target_bearing_deg": 0.0},
                    {"t_ns": 1_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 0.0},
                    {"t_ns": 2_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 0.0},
                    {"t_ns": 3_000_000_000, "state": "LOCKED", "target_id": "speaker-a", "target_bearing_deg": 0.0},
                ],
            )
            _write_jsonl(candidate_run / "traces" / "faces.jsonl", [{"t_ns": 1_000_000_000, "faces": [{}]}])
            manifest.write_text(
                "\n".join(
                    [
                        "scenes:",
                        "  - scene_id: synthetic_scene",
                        "    target_angle_deg: 0",
                        "    interferer_angle_deg: 120",
                        f"    target_reference_wav: {root / 'refs' / 'target.wav'}",
                        f"    interferer_reference_wav: {root / 'refs' / 'interferer.wav'}",
                        f"    baseline_audio_path: {baseline_run / 'audio' / 'enhanced.wav'}",
                        f"    candidate_audio_path: {candidate_run / 'audio' / 'enhanced.wav'}",
                        "    reference_text: hello world from focus field",
                        "    baseline_text: hello word from focus",
                        "    candidate_text: hello world from focus field",
                        "    labels:",
                        "      speaker_segments:",
                        "        - start_s: 1.0",
                        "          end_s: 2.0",
                        "          speaker_id: speaker-a",
                        "        - start_s: 2.0",
                        "          end_s: 3.0",
                        "          speaker_id: speaker-a",
                        "      bearing_segments:",
                        "        - start_s: 1.0",
                        "          end_s: 2.0",
                        "          bearing_deg: 0.0",
                        "        - start_s: 2.0",
                        "          end_s: 3.0",
                        "          bearing_deg: 0.0",
                        "      track_segments:",
                        "        - start_s: 1.0",
                        "          end_s: 2.0",
                        "          speaker_id: speaker-a",
                        "          track_id: track-a",
                        "        - start_s: 2.0",
                        "          end_s: 3.0",
                        "          speaker_id: speaker-a",
                        "          track_id: track-a",
                        "      face_segments:",
                        "        - start_s: 0.0",
                        "          end_s: 0.5",
                        "          present: false",
                        "        - start_s: 0.5",
                        "          end_s: 1.5",
                        "          present: true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = run_focusbench(str(baseline_run), str(candidate_run), str(manifest), str(out_dir), strict_truth=True)
            gates = report["summary"]["gates"]
            names = {check["name"] for check in gates["checks"]}
            self.assertIn("lock_jitter_rms", names)
            self.assertIn("handoff_latency_p95_ms", names)
            self.assertIn("false_handoff_rate", names)
            self.assertIn("no_lock_during_speech_ratio", names)
            self.assertIn("output_underrun_rate", names)
            self.assertIn("queue_pressure", names)
            self.assertIn("speaker_selection_accuracy", names)
            self.assertIn("steering_mae_deg", names)
            self.assertIn("face_reacquire_latency_p95_ms", names)
            self.assertIn("id_churn_rate", names)
            self.assertTrue(gates["passed"])
            self.assertIn("label_scene_metrics", report)
            self.assertEqual(report["summary"]["labels"]["label_supported_scene_count"], 1.0)
            self.assertIsNotNone(report["summary"]["labels"]["speaker_selection_accuracy"])

    def test_run_focusbench_accepts_canonical_reference_audio_and_scene_clip_window(self) -> None:
        sr = 16000
        n = sr
        t = np.arange(n, dtype=np.float32) / float(sr)
        target = 0.35 * np.sin(2.0 * np.pi * 220.0 * t)
        interferer = 0.25 * np.sin(2.0 * np.pi * 540.0 * t + 0.33)
        target_full = np.concatenate([target, target])
        interferer_full = np.concatenate([interferer, interferer])
        baseline = np.concatenate([target + 0.4 * interferer, target + 0.7 * interferer])
        candidate = np.concatenate([target + 0.8 * interferer, target + 0.05 * interferer])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_run = root / "baseline"
            candidate_run = root / "candidate"
            manifest = root / "scenes.yaml"
            out_dir = root / "out"
            ref_dir = root / "refs"
            _write_wav(ref_dir / "target.wav", target_full, sr)
            _write_wav(ref_dir / "noise.wav", interferer_full, sr)
            _write_wav(baseline_run / "audio" / "enhanced.wav", baseline, sr)
            _write_wav(candidate_run / "audio" / "enhanced.wav", candidate, sr)
            _write_jsonl(
                candidate_run / "logs" / "perf.jsonl",
                [
                    {
                        "t_ns": 1_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 95.0},
                        "queue_pressure": {"drop_total_window": 1},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 4, "buffer_capacity_frames": 16},
                    },
                    {
                        "t_ns": 3_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 102.0},
                        "queue_pressure": {"drop_total_window": 1},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 5, "buffer_capacity_frames": 16},
                    },
                ],
            )
            _write_jsonl(candidate_run / "logs" / "events.jsonl", [])
            _write_jsonl(candidate_run / "traces" / "lock.jsonl", [])
            _write_jsonl(candidate_run / "traces" / "faces.jsonl", [])
            manifest.write_text(
                "\n".join(
                    [
                        "scenes:",
                        "  - scene_id: clipped_scene",
                        "    start_s: 1.0",
                        "    end_s: 2.0",
                        f"    audio_path: {candidate_run / 'audio' / 'enhanced.wav'}",
                        f"    reference_audio_path: {ref_dir / 'target.wav'}",
                        f"    noise_reference_audio_path: {ref_dir / 'noise.wav'}",
                        f"    baseline_audio_path: {baseline_run / 'audio' / 'enhanced.wav'}",
                        f"    candidate_audio_path: {candidate_run / 'audio' / 'enhanced.wav'}",
                        "    video_paths: []",
                        "    speaker_segments: []",
                        "    bearing_segments: []",
                        "    tags: [demo, ab]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = run_focusbench(str(baseline_run), str(candidate_run), str(manifest), str(out_dir))

            self.assertGreater(report["summary"]["quality"]["median_si_sdr_delta_db"] or 0.0, 0.0)
            self.assertGreater(report["summary"]["quality"]["median_stoi_delta"] or 0.0, 0.0)
            self.assertGreater(report["summary"]["quality"]["median_sir_delta_db"] or 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
