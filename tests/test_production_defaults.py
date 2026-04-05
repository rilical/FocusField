import copy
import unittest

from focusfield.core.config import load_config


class ProductionDefaultTests(unittest.TestCase):
    def test_meeting_peripheral_config_is_explicit_three_camera_eight_mic_profile(self) -> None:
        cfg = load_config("configs/meeting_peripheral.yaml")
        self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
        self.assertEqual(cfg["audio"]["channels"], 8)
        self.assertEqual(cfg["audio"]["device_profile"], "minidsp_uma8_raw_7p1")
        self.assertEqual(len(cfg["video"]["cameras"]), 3)
        self.assertEqual(cfg["output"]["sink"], "usb_mic")
        self.assertFalse(bool(cfg["runtime"]["fail_fast"]))

    def test_demo_safe_profile_prefers_stability_and_evidence_collection(self) -> None:
        cfg = load_config("configs/meeting_peripheral_demo_safe.yaml")
        self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
        self.assertTrue(bool(cfg["trace"]["enabled"]))
        self.assertTrue(bool(cfg["fusion"]["require_vad"]))
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertEqual(cfg["fusion"]["thresholds_preset"], "balanced")
        self.assertEqual(cfg["fusion"]["audio_fallback"]["score_mode"], "confidence")
        self.assertFalse(bool(cfg["ui"]["enabled"]))

    def test_demo_ui_profile_enables_low_rate_ui_without_changing_demo_safe_defaults(self) -> None:
        safe_cfg = load_config("configs/meeting_peripheral_demo_safe.yaml")
        demo_cfg = load_config("configs/meeting_peripheral_demo_ui.yaml")

        self.assertTrue(bool(demo_cfg["ui"]["enabled"]))
        self.assertEqual(demo_cfg["ui"]["telemetry_hz"], 1)
        self.assertEqual(demo_cfg["ui"]["frame_max_hz"], 0.5)
        self.assertEqual(demo_cfg["ui"]["frame_jpeg_quality"], 50)
        self.assertEqual(
            demo_cfg["output"]["usb_mic"]["device_selector"]["exact_name"],
            "FocusField USB Mic",
        )
        self.assertFalse(demo_cfg["output"]["usb_mic"]["device_selector"].get("match_substring"))

        normalized_safe = copy.deepcopy(safe_cfg)
        normalized_demo = copy.deepcopy(demo_cfg)
        normalized_safe["runtime"].pop("config_path", None)
        normalized_safe["runtime"].pop("config_basename", None)
        normalized_demo["runtime"].pop("config_path", None)
        normalized_demo["runtime"].pop("config_basename", None)
        normalized_demo["ui"] = normalized_safe["ui"]
        normalized_safe["output"]["usb_mic"]["device_selector"]["match_substring"] = None
        normalized_demo["output"]["usb_mic"]["device_selector"].pop("exact_name", None)
        self.assertEqual(normalized_demo, normalized_safe)


if __name__ == "__main__":
    unittest.main()
