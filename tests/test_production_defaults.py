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


if __name__ == "__main__":
    unittest.main()
