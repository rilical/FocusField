import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from focusfield.bench.profile_loader import (
    default_pi_nightly_profile_path,
    load_focusbench_thresholds,
    load_pi_perf_gate_thresholds,
)


def _load_script_module(module_name: str, relative_path: str):
    script_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load script module: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchEntrypointTests(unittest.TestCase):
    def test_default_profile_path_is_repo_local(self) -> None:
        path = default_pi_nightly_profile_path()
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "full_3cam_8mic_pi_prod.yaml")

    def test_profile_loader_merges_nested_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_path = Path(tmpdir) / "profile.yaml"
            profile_path.write_text(
                "\n".join(
                    [
                        "bench:",
                        "  targets:",
                        "    latency_p95_ms_max: 123.0",
                        "    audio_queue_full_max: 7",
                        "pi_perf_gate:",
                        "  queue_full_max: 9",
                        "  min_runtime_seconds: 12",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            focusbench = load_focusbench_thresholds(profile_path)
            perf_gate = load_pi_perf_gate_thresholds(profile_path)

        self.assertEqual(focusbench["latency_p95_ms_max"], 123.0)
        self.assertEqual(focusbench["audio_queue_full_max"], 7.0)
        self.assertEqual(perf_gate["queue_full_max"], 9.0)
        self.assertEqual(perf_gate["min_runtime_seconds"], 12.0)

    def test_focusbench_ab_main_merges_config_and_profile_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            baseline = tmp_path / "baseline"
            candidate = tmp_path / "candidate"
            scene_manifest = tmp_path / "scenes.yaml"
            output_dir = tmp_path / "out"
            baseline.mkdir()
            candidate.mkdir()
            scene_manifest.write_text("scenes: []\n", encoding="utf-8")

            captured = {}
            focusbench_ab = _load_script_module("focusbench_ab_test", "scripts/focusbench_ab.py")

            def _run_focusbench(*, baseline_run, candidate_run, scene_manifest, output_dir, thresholds):  # noqa: ANN001
                captured["thresholds"] = dict(thresholds)
                captured["baseline_run"] = baseline_run
                captured["candidate_run"] = candidate_run
                captured["scene_manifest"] = scene_manifest
                captured["output_dir"] = output_dir
                return {"summary": {"gates": {"passed": True}}, "output_path": f"{output_dir}/BenchReport.json"}

            with (
                patch.object(focusbench_ab, "load_config", return_value={"bench": {"targets": {"latency_p95_ms_max": 111.0, "audio_queue_full_max": 17.0}}}),
                patch.object(focusbench_ab, "load_focusbench_thresholds", return_value={"latency_p95_ms_max": 222.0, "queue_pressure_max": 8.0}),
                patch.object(focusbench_ab, "run_focusbench", side_effect=_run_focusbench),
                patch.object(sys, "argv", [
                    "focusbench_ab.py",
                    "--baseline-run",
                    str(baseline),
                    "--candidate-run",
                    str(candidate),
                    "--scene-manifest",
                    str(scene_manifest),
                    "--output-dir",
                    str(output_dir),
                ]),
            ):
                rc = focusbench_ab.main()

        self.assertEqual(rc, 0)
        self.assertEqual(captured["thresholds"]["latency_p95_ms_max"], 222.0)
        self.assertEqual(captured["thresholds"]["audio_queue_full_max"], 17.0)
        self.assertEqual(captured["thresholds"]["queue_pressure_max"], 8.0)

    def test_pi_perf_gate_main_runs_from_clean_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            logs_dir = run_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            perf_path = logs_dir / "perf.jsonl"
            events_path = logs_dir / "events.jsonl"
            perf_path.write_text(
                "\n".join(
                    [
                        '{"t_ns": 1000000000, "enhanced_final": {"pipeline_queue_age_ms": 50.0}, "audio_capture": {"status_input_overflow_total": 1}, "bus": {"publish_delta": {"fusion.candidates": 10}}, "worker_loops": {"vision.face_track.cam0": {"t_ns": 1000000000, "processed_cycles": 1}}}',
                        '{"t_ns": 2000000000, "enhanced_final": {"pipeline_queue_age_ms": 55.0}, "audio_capture": {"status_input_overflow_total": 2}, "bus": {"publish_delta": {"fusion.candidates": 10}}, "worker_loops": {"vision.face_track.cam0": {"t_ns": 2000000000, "processed_cycles": 2}}}',
                        '{"t_ns": 3000000000, "enhanced_final": {"pipeline_queue_age_ms": 60.0}, "audio_capture": {"status_input_overflow_total": 2}, "bus": {"publish_delta": {"fusion.candidates": 10}}, "worker_loops": {"vision.face_track.cam0": {"t_ns": 3000000000, "processed_cycles": 3}}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            events_path.write_text(
                "\n".join(
                    [
                        '{"context": {"module": "fusion.av_association", "event": "no_candidates", "details": {"vad_speech": false, "reason": "no_faces_audio_fallback"}}}',
                        '{"context": {"module": "fusion.av_association", "event": "no_candidates", "details": {"vad_speech": false, "reason": "no_faces_audio_fallback"}}}',
                        '{"context": {"module": "fusion.av_association", "event": "no_candidates", "details": {"vad_speech": false, "reason": "no_faces_audio_fallback"}}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            pi_perf_gate = _load_script_module("pi_perf_gate_test", "scripts/pi_perf_gate.py")
            with (
                patch.object(pi_perf_gate, "load_pi_perf_gate_thresholds", return_value={
                    "latency_p95_max": 100.0,
                    "latency_p99_max": 100.0,
                    "overflow_delta_max": 10,
                    "queue_full_max": 10,
                    "no_candidates_ratio_max": 1.0,
                    "speech_with_no_lock_ratio_max": 1.0,
                    "no_faces_fallback_ratio_max": 1.0,
                    "overflow_rate_max_per_min": 100.0,
                    "face_track_rate_min": 0.0,
                    "face_detection_stall_max_ms": 10000.0,
                    "lock_continuity_ratio_min": 0.0,
                    "min_runtime_seconds": 1.0,
                    "no_candidates_denominator_min": 1,
                }),
                patch.object(sys, "argv", ["pi_perf_gate.py", "--run-dir", str(run_dir)]),
            ):
                rc = pi_perf_gate.main()

        self.assertEqual(rc, 0)

    def test_entrypoint_help_commands_exit_cleanly(self) -> None:
        scripts = [
            Path("scripts/focusbench_ab.py"),
            Path("scripts/pi_perf_gate.py"),
            Path("scripts/demo_benchmark_pipeline.py"),
        ]
        for script in scripts:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
