import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.calibration_workflow import build_calibration_plan
from scripts.provision_focusfield import build_provision_plan
from scripts.recover_focusfield import build_recovery_plan
from scripts.support_bundle import create_support_bundle


class SupportWorkflowTests(unittest.TestCase):
    def test_support_bundle_writes_summary_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "artifacts" / "run_001"
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "logs" / "perf.jsonl").write_text('{"t_ns": 1, "queue_pressure": {"drop_total_window": 0}}\n', encoding="utf-8")
            (run_dir / "logs" / "events.jsonl").write_text('{"t_ns": 1, "context": {"event": "ok"}}\n', encoding="utf-8")
            (run_dir / "run_meta.json").write_text('{"git": {"commit": "abc123"}}', encoding="utf-8")
            (run_dir / "config_effective.yaml").write_text("runtime:\n  mode: meeting_peripheral\n", encoding="utf-8")
            config_path = root / "meeting.yaml"
            config_path.write_text("runtime:\n  mode: meeting_peripheral\nvideo:\n  cameras:\n    - id: cam0\n", encoding="utf-8")
            (root / "camera_calibration.json").write_text(json.dumps({"cameras": [{"id": "cam0", "yaw_offset_deg": 5.0}]}), encoding="utf-8")
            output = root / "support_bundle.zip"

            bundle = create_support_bundle(run_dir, output, config_path=str(config_path), service_name="focusfield")
            self.assertEqual(bundle, output.resolve())
            with zipfile.ZipFile(output) as zf:
                names = set(zf.namelist())
                self.assertIn("summary.json", names)
                self.assertIn("run/run_meta.json", names)
                self.assertIn("run/logs/perf.jsonl", names)
                summary = json.loads(zf.read("summary.json").decode("utf-8"))
                self.assertTrue(summary["files"]["run_meta"])

    def test_provision_plan_defaults_to_meeting_peripheral_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "meeting.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: meeting_peripheral",
                        "video:",
                        "  cameras:",
                        "    - id: cam0",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            plan = build_provision_plan(str(config_path), service_name="focusfield")
            self.assertEqual(plan["mode"], "meeting_peripheral")
            self.assertTrue(plan["audio_only_boot"])
            self.assertIn("--audio-only", plan["commands"]["preflight"])

    def test_calibration_plan_flags_missing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "meeting.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "audio:",
                        "  device_profile: minidsp_uma8_raw_7p1",
                        "video:",
                        "  cameras:",
                        "    - id: cam0",
                        "      yaw_offset_deg: 0",
                        "    - id: cam1",
                        "      yaw_offset_deg: 120",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            plan = build_calibration_plan(str(config_path))
            self.assertTrue(plan["needs_camera_calibration"])
            self.assertIn("cam0", plan["camera_ids"])

    def test_recovery_plan_detects_queue_pressure_and_missing_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "artifacts" / "run_001"
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "logs" / "perf.jsonl").write_text(
                '{"t_ns": 1, "queue_pressure": {"drop_total_window": 3}, "audio_output": {"underrun_total": 1}}\n',
                encoding="utf-8",
            )
            (run_dir / "logs" / "events.jsonl").write_text(
                '{"t_ns": 1, "context": {"event": "camera_missing"}}\n',
                encoding="utf-8",
            )
            config_path = root / "meeting.yaml"
            config_path.write_text("video:\n  cameras:\n    - id: cam0\naudio:\n  device_profile: minidsp_uma8_raw_7p1\n", encoding="utf-8")

            plan = build_recovery_plan(str(config_path), run_dir=str(run_dir), service_name="focusfield")
            self.assertIn("queue_pressure", plan["issues"])
            self.assertIn("camera_calibration_missing", plan["issues"])
            self.assertTrue(any("support_bundle.py" in action for action in plan["actions"]))


if __name__ == "__main__":
    unittest.main()
