import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.demo_ab_capture import build_demo_capture_bundle, write_demo_capture_bundle
from scripts.demo_panel_report import build_demo_panel_report, write_demo_panel_report
from scripts.demo_rehearsal_gate import build_demo_readiness


def _write_wav(path: Path, data: np.ndarray, sample_rate: int = 16000) -> None:
    x = np.asarray(data, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    pcm16 = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
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


class DemoWorkflowTests(unittest.TestCase):
    def test_demo_rehearsal_gate_passes_with_zoom_evidence_and_healthy_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "artifacts" / "run_001"
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "crash").mkdir(parents=True, exist_ok=True)
            _write_jsonl(
                run_dir / "logs" / "perf.jsonl",
                [
                    {
                        "t_ns": 1_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 95.0},
                        "queue_pressure": {"drop_total_window": 2},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 4, "buffer_capacity_frames": 16},
                    },
                    {
                        "t_ns": 1_901_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 105.0},
                        "queue_pressure": {"drop_total_window": 3},
                        "output": {"underrun_window": 0, "underrun_total": 0, "occupancy_frames": 5, "buffer_capacity_frames": 16},
                    },
                ],
            )
            _write_jsonl(run_dir / "logs" / "events.jsonl", [])
            evidence_path = root / "host_gate.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "cold_boot": {
                            "host_visible_microphone": True,
                            "boot_time_s": 9.4,
                            "device_name": "FocusField USB Mic",
                            "artifact_path": str(root / "boot.json"),
                        },
                        "reconnect": {
                            "recovered": True,
                            "reconnect_time_s": 2.8,
                            "artifact_path": str(root / "reconnect.json"),
                        },
                        "meeting_apps": [
                            {
                                "app": "Zoom",
                                "artifact_path": str(root / "zoom.json"),
                                "selected_input_device": "FocusField USB Mic",
                                "duration_s": 1800,
                                "verdict": "pass",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_demo_readiness(
                "configs/meeting_peripheral_demo_safe.yaml",
                run_dir=str(run_dir),
                host_gate_evidence=str(evidence_path),
            )

            self.assertTrue(report["passed"])
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["summary"]["zoom_selected_input_device"], "FocusField USB Mic")
            self.assertGreater(report["summary"]["soak"]["duration_s"], 1800.0)

    def test_demo_rehearsal_gate_fails_on_short_soak_and_runtime_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "artifacts" / "run_001"
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "crash").mkdir(parents=True, exist_ok=True)
            (run_dir / "crash" / "crash.txt").write_text("boom", encoding="utf-8")
            _write_jsonl(
                run_dir / "logs" / "perf.jsonl",
                [
                    {
                        "t_ns": 1_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 95.0},
                        "queue_pressure": {"drop_total_window": 40},
                        "output": {"underrun_window": 2, "underrun_total": 2, "occupancy_frames": 14, "buffer_capacity_frames": 16},
                    },
                    {
                        "t_ns": 61_000_000_000,
                        "enhanced_final": {"pipeline_queue_age_ms": 260.0},
                        "queue_pressure": {"drop_total_window": 40},
                        "output": {"underrun_window": 2, "underrun_total": 4, "occupancy_frames": 15, "buffer_capacity_frames": 16},
                    },
                ],
            )
            _write_jsonl(run_dir / "logs" / "events.jsonl", [])
            evidence_path = root / "host_gate.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "cold_boot": {"host_visible_microphone": True, "boot_time_s": 9.4, "artifact_path": str(root / "boot.json")},
                        "reconnect": {"recovered": True, "reconnect_time_s": 2.8, "artifact_path": str(root / "reconnect.json")},
                        "meeting_apps": [
                            {
                                "app": "Zoom",
                                "artifact_path": str(root / "zoom.json"),
                                "selected_input_device": "FocusField USB Mic",
                                "duration_s": 600,
                                "verdict": "pass",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_demo_readiness(
                "configs/meeting_peripheral_demo_safe.yaml",
                run_dir=str(run_dir),
                host_gate_evidence=str(evidence_path),
            )

            self.assertFalse(report["passed"])
            failed = {item["name"] for item in report["checks"] if not item["passed"]}
            self.assertIn("latency_p99_ms", failed)
            self.assertIn("queue_pressure_peak", failed)
            self.assertIn("crash_free_soak", failed)

    def test_demo_ab_capture_writes_bundle_and_manifest_in_dry_run_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "artifacts" / "run_001"
            (run_dir / "audio").mkdir(parents=True, exist_ok=True)
            baseline = root / "baseline.wav"
            reference = root / "reference.wav"
            signal = 0.1 * np.sin(2.0 * np.pi * 220.0 * np.arange(16000, dtype=np.float32) / 16000.0)
            _write_wav(run_dir / "audio" / "enhanced.wav", signal)
            _write_wav(baseline, signal)
            _write_wav(reference, signal)
            scene_spec = root / "scenes.yaml"
            scene_spec.write_text(
                "\n".join(
                    [
                        "scenes:",
                        "  - scene_id: hvac_turn",
                        "    start_s: 0.2",
                        "    end_s: 0.8",
                        "    tags: [hvac, profile_turn]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_demo_capture_bundle(
                candidate_run=str(run_dir),
                baseline_audio_path=str(baseline),
                reference_audio_path=str(reference),
                output_dir=str(root / "bundle"),
                scene_spec_path=str(scene_spec),
                dry_run=True,
            )
            written = write_demo_capture_bundle(payload, root / "bundle")

            self.assertTrue(written["bundle_path"].exists())
            self.assertTrue(written["manifest_path"].exists())
            manifest = yaml.safe_load(written["manifest_path"].read_text(encoding="utf-8"))
            scene = manifest["scenes"][0]
            self.assertEqual(scene["scene_id"], "hvac_turn")
            self.assertEqual(scene["reference_audio_path"], str(reference.resolve()))
            self.assertEqual(scene["start_s"], 0.2)
            self.assertEqual(scene["end_s"], 0.8)
            self.assertTrue(payload["bundle"]["missing_artifacts"])

    def test_demo_panel_report_writes_scorecard_and_copies_plots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plot = root / "latency.png"
            plot.write_bytes(b"plot")
            bench = root / "BenchReport.json"
            bench.write_text(
                json.dumps(
                    {
                        "summary": {
                            "quality": {
                                "median_si_sdr_delta_db": 3.2,
                                "median_stoi_delta": 0.08,
                                "median_wer_relative_improvement": 0.24,
                                "median_sir_delta_db": 6.1,
                            },
                            "latency": {"p50_ms": 88.0, "p95_ms": 120.0, "p99_ms": 140.0},
                            "runtime": {"output_underrun_rate": 0.0, "output_underrun_total": 0, "queue_pressure_peak": 3.0},
                            "gates": {"passed": True},
                        },
                        "plots": {"latency_quantiles": str(plot)},
                    }
                ),
                encoding="utf-8",
            )
            readiness = root / "demo_readiness.json"
            readiness.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "summary": {
                            "boot_to_host_visible_mic_s": 8.4,
                            "reconnect_time_s": 2.6,
                            "zoom_selected_input_device": "FocusField USB Mic",
                            "soak": {"passed": True, "duration_s": 1900.0},
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = build_demo_panel_report(str(bench), demo_readiness_path=str(readiness))
            written = write_demo_panel_report(payload, root / "panel")

            self.assertTrue(written["json_path"].exists())
            self.assertTrue(written["markdown_path"].exists())
            copied_plot = Path(json.loads(written["json_path"].read_text(encoding="utf-8"))["plots"]["latency_quantiles"])
            self.assertTrue(copied_plot.exists())
            markdown = written["markdown_path"].read_text(encoding="utf-8")
            self.assertIn("FocusField Demo Panel Scorecard", markdown)
            self.assertIn("Latency p95", markdown)


if __name__ == "__main__":
    unittest.main()
