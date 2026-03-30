import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.meeting_host_gate import build_host_gate_report, load_host_gate_evidence  # noqa: E402


class MeetingHostGateTests(unittest.TestCase):
    def test_dry_run_separates_assumptions_from_proven_checks(self) -> None:
        report = build_host_gate_report("configs/meeting_peripheral.yaml", dry_run=True)
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertFalse(report["passed"])
        self.assertTrue(report["dry_run"])
        self.assertGreaterEqual(len(report["assumptions"]), 1)
        self.assertTrue(all(check["status"] == "ASSUMED" for check in report["checks"]))
        self.assertIn("meeting_app_artifact_shape", report)
        self.assertIn("Zoom", report["meeting_app_artifact_shape"]["required_apps"])

    def test_evidence_verdict_passes_with_host_visible_mic_and_app_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_path = root / "meeting_host_gate.evidence.json"
            evidence = {
                "cold_boot": {
                    "host_visible_microphone": True,
                    "boot_time_s": 31.4,
                    "device_name": "FocusField USB Mic",
                    "artifact_path": str(root / "boot.json"),
                },
                "reconnect": {
                    "recovered": True,
                    "reconnect_time_s": 4.2,
                    "artifact_path": str(root / "reconnect.json"),
                },
                "meeting_apps": [
                    {
                        "app": "Zoom",
                        "artifact_path": str(root / "zoom.json"),
                        "selected_input_device": "FocusField USB Mic",
                        "duration_s": 3600,
                        "verdict": "pass",
                    },
                    {
                        "app": "Google Meet",
                        "artifact_path": str(root / "meet.json"),
                        "selected_input_device": "FocusField USB Mic",
                        "duration_s": 3600,
                        "verdict": "pass",
                    },
                    {
                        "app": "Microsoft Teams",
                        "artifact_path": str(root / "teams.json"),
                        "selected_input_device": "FocusField USB Mic",
                        "duration_s": 3600,
                        "verdict": "pass",
                    },
                ],
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            report = build_host_gate_report("configs/meeting_peripheral.yaml", str(evidence_path))
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(report["passed"])
            proven_names = {check["name"] for check in report["proven_checks"]}
            self.assertEqual(
                proven_names,
                {"cold_boot_host_visible_mic", "reconnect_recovery", "meeting_app_verdict_artifacts"},
            )
            meeting_app_check = next(check for check in report["checks"] if check["name"] == "meeting_app_verdict_artifacts")
            self.assertEqual(len(meeting_app_check["evidence"]["received_apps"]), 3)
            self.assertEqual(meeting_app_check["evidence"]["required_count"], 3)

    def test_malformed_meeting_app_artifact_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_path = root / "meeting_host_gate.evidence.json"
            evidence = {
                "cold_boot": {
                    "host_visible_microphone": True,
                    "boot_time_s": 29.0,
                    "artifact_path": str(root / "boot.json"),
                },
                "reconnect": {
                    "recovered": True,
                    "reconnect_time_s": 2.0,
                    "artifact_path": str(root / "reconnect.json"),
                },
                "meeting_apps": [
                    {
                        "app": "Zoom",
                        "selected_input_device": "FocusField USB Mic",
                        "duration_s": 3600,
                        "verdict": "pass",
                    }
                ],
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            report = build_host_gate_report("configs/meeting_peripheral.yaml", str(evidence_path))
            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["passed"])
            self.assertTrue(report["validation_errors"])
            self.assertTrue(any("missing keys" in error for error in report["validation_errors"]))
            meeting_app_check = next(check for check in report["checks"] if check["name"] == "meeting_app_verdict_artifacts")
            self.assertEqual(meeting_app_check["status"], "FAILED")
            self.assertFalse(meeting_app_check["passed"])

    def test_load_host_gate_evidence_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(["bad"]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_host_gate_evidence(evidence_path)


if __name__ == "__main__":
    unittest.main()
