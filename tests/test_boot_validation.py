import tempfile
import unittest
import zipfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.boot_validation import boot_plan, validate_local_model_assets


class BootValidationTests(unittest.TestCase):
    def test_boot_plan_marks_fast_boot_modes_audio_only(self) -> None:
        cfg = {
            "runtime": {
                "mode": "meeting_peripheral",
                "startup": {"validate_runtime_models": True},
                "requirements": {"min_cameras": 3, "min_audio_channels": 8, "camera_scope": "usb"},
            }
        }
        plan = boot_plan(cfg)
        self.assertTrue(plan["audio_only"])
        self.assertEqual(plan["require_cameras"], 0)
        self.assertEqual(plan["require_audio_channels"], 1)
        self.assertEqual(plan["camera_source"], "auto")
        self.assertEqual(plan["camera_scope"], "any")

    def test_validate_local_model_assets_requires_bundled_assets_when_downloads_disabled(self) -> None:
        cfg = {
            "runtime": {"mode": "appliance_fastboot"},
            "vision": {
                "models": {"allow_runtime_downloads": False},
                "face": {"backend": "yunet", "yunet_model_path": ""},
                "mouth": {"backend": "tflite", "use_facemesh": True, "mesh_model_path": "", "tflite_model_path": ""},
            },
            "audio": {"models": {"allow_runtime_downloads": False}},
        }
        errors = validate_local_model_assets(cfg, "/tmp/focusfield.yaml")
        self.assertTrue(any("vision.face.yunet_model_path" in err for err in errors))
        self.assertTrue(any("vision.mouth" in err for err in errors))

    def test_validate_local_model_assets_accepts_bundled_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            yunet = root / "yunet.onnx"
            yunet.write_bytes(b"fake-yunet")
            task = root / "face_landmarker.task"
            with zipfile.ZipFile(task, "w") as zf:
                zf.writestr("face_landmarks_detector.tflite", b"fake-tflite")
            cfg = {
                "runtime": {"mode": "meeting_peripheral"},
                "vision": {
                    "models": {"allow_runtime_downloads": False},
                    "face": {"backend": "yunet", "yunet_model_path": yunet.name},
                    "mouth": {"backend": "tflite", "use_facemesh": True, "mesh_model_path": task.name, "tflite_model_path": ""},
                },
                "audio": {"models": {"allow_runtime_downloads": False}},
            }
            errors = validate_local_model_assets(cfg, str(root / "config.yaml"))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
