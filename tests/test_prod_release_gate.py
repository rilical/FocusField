import argparse
import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.prod_release_gate as prod_release_gate


class ProdReleaseGateTests(unittest.TestCase):
    def test_release_gate_runs_checks_in_order_and_reports_verdict(self) -> None:
        calls = []

        def _pass(name):
            def _inner(*_args, **_kwargs):
                calls.append(name)
                return prod_release_gate.ReleaseGateResult(name=name, passed=True, details={"name": name})

            return _inner

        args = argparse.Namespace(
            config="configs/full_3cam_8mic_pi_prod.yaml",
            baseline_run="baseline",
            candidate_run="candidate",
            scene_manifest="manifest.yaml",
            output_dir="out",
            host_gate_evidence="host.json",
            pytest_targets=["tests"],
            allow_ad_hoc_quality=False,
        )

        with (
            patch.object(prod_release_gate, "load_config", return_value={"runtime": {"mode": "meeting_peripheral"}, "bench": {"targets": {}}}),
            patch.object(prod_release_gate, "_run_pytest_suite", side_effect=_pass("unit_and_integration_tests")),
            patch.object(prod_release_gate, "_run_boot_validation", side_effect=_pass("boot_validation")),
            patch.object(prod_release_gate, "_run_pi_perf_gate", side_effect=_pass("pi_perf_gate")),
            patch.object(prod_release_gate, "_run_host_gate", side_effect=_pass("host_meeting_gate")),
            patch.object(prod_release_gate, "_run_focusbench_gate", side_effect=_pass("focusbench")),
        ):
            report = prod_release_gate.run_release_gate(args)

        self.assertTrue(report["passed"])
        self.assertEqual(
            calls,
            ["unit_and_integration_tests", "boot_validation", "pi_perf_gate", "host_meeting_gate", "focusbench"],
        )
        self.assertEqual([item["name"] for item in report["checks"]], calls)

    def test_focusbench_gate_rejects_missing_quality_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline"
            candidate = root / "candidate"
            manifest = root / "scenes.yaml"
            output_dir = root / "out"
            baseline.mkdir(parents=True, exist_ok=True)
            candidate.mkdir(parents=True, exist_ok=True)
            manifest.write_text("scenes: []\n", encoding="utf-8")

            fake_report = {
                "output_path": str(output_dir / "BenchReport.json"),
                "summary": {
                    "gates": {"passed": True, "checks": []},
                    "quality": {
                        "median_si_sdr_delta_db": None,
                        "median_stoi_delta": None,
                        "median_wer_relative_improvement": None,
                        "median_sir_delta_db": None,
                    },
                },
            }

            with patch.object(prod_release_gate, "run_focusbench", return_value=fake_report):
                result = prod_release_gate._run_focusbench_gate(
                    baseline_run=str(baseline),
                    candidate_run=str(candidate),
                    scene_manifest=str(manifest),
                    output_dir=str(output_dir),
                    thresholds={},
                    require_truth=True,
                )

        self.assertFalse(result.passed)
        self.assertEqual(
            set(result.details["missing_quality_metrics"]),
            {"median_si_sdr_delta_db", "median_stoi_delta", "median_wer_relative_improvement", "median_sir_delta_db"},
        )

    def test_host_gate_requires_evidence_file(self) -> None:
        result = prod_release_gate._run_host_gate("/tmp/definitely-missing-host-gate.json")
        self.assertFalse(result.passed)
        self.assertEqual(result.details["error"], "missing_evidence")

    def test_host_gate_accepts_passing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = Path(tmpdir) / "host_gate.json"
            evidence.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "host_visible_mic": True,
                        "checks": [{"name": "cold_boot", "passed": True}, {"name": "replug", "passed": True}],
                    }
                ),
                encoding="utf-8",
            )
            result = prod_release_gate._run_host_gate(str(evidence))
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
