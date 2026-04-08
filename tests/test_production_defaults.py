import unittest
import tempfile
from pathlib import Path

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
        self.assertEqual(int(cfg["audio"]["block_size"]), 2048)
        self.assertFalse(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(int(cfg["video"]["cameras"][0]["fps"]), 6)
        self.assertEqual(int(cfg["vision"]["face"]["detect_every_n"]), 2)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.0)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.frames"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertFalse(bool(cfg["uma8_leds"]["enabled"]))

    def test_demo_safe_profile_prefers_stability_and_evidence_collection(self) -> None:
        cfg = load_config("configs/meeting_peripheral_demo_safe.yaml")
        self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
        self.assertTrue(bool(cfg["trace"]["enabled"]))
        self.assertTrue(bool(cfg["fusion"]["require_vad"]))
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertEqual(cfg["fusion"]["thresholds_preset"], "balanced")
        self.assertEqual(cfg["fusion"]["audio_fallback"]["score_mode"], "confidence")
        self.assertFalse(bool(cfg["ui"]["enabled"]))

    def test_pi_demo_live_profile_is_trimmed_for_single_speaker_stability(self) -> None:
        cfg = load_config("configs/full_3cam_8mic_pi_demo_live.yaml")
        self.assertFalse(bool(cfg["trace"]["enabled"]))
        self.assertFalse(bool(cfg["ui"]["enabled"]))
        self.assertFalse(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertFalse(bool(cfg["fusion"]["require_vad"]))
        self.assertFalse(bool(cfg["fusion"]["require_speaking"]))
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(int(cfg["audio"]["block_size"]), 2048)
        self.assertEqual(int(cfg["audio"]["capture"]["queue_depth"]), 16)
        self.assertEqual(int(cfg["audio"]["vad"]["update_hz"]), 4)
        self.assertEqual(int(cfg["video"]["cameras"][0]["fps"]), 6)
        self.assertEqual(int(cfg["vision"]["face"]["detect_every_n"]), 2)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.0)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.vad"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertEqual(str(cfg["output"]["sink"]), "none")
        self.assertFalse(bool(cfg["uma8_leds"]["enabled"]))

    def test_pi_demo_observe_profile_keeps_low_rate_ui_without_trace(self) -> None:
        cfg = load_config("configs/full_3cam_8mic_pi_demo_observe.yaml")
        self.assertFalse(bool(cfg["trace"]["enabled"]))
        self.assertTrue(bool(cfg["ui"]["enabled"]))
        self.assertFalse(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(int(cfg["ui"]["telemetry_hz"]), 1)
        self.assertEqual(int(cfg["ui"]["frame_max_hz"]), 1)
        self.assertFalse(bool(cfg["fusion"]["require_vad"]))
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(int(cfg["audio"]["vad"]["update_hz"]), 4)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.0)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.vad"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)

    def test_threshold_preset_preserves_explicit_threshold_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "thresholds_presets.yaml").write_text(
                "balanced:\n"
                "  acquire_threshold: 0.65\n"
                "  hold_ms: 800\n"
                "  min_switch_interval_ms: 500\n",
                encoding="utf-8",
            )
            (root / "config.yaml").write_text(
                "fusion:\n"
                "  thresholds_preset: balanced\n"
                "  thresholds:\n"
                "    acquire_threshold: 0.20\n"
                "    hold_ms: 1500\n",
                encoding="utf-8",
            )
            cfg = load_config(str(root / "config.yaml"))
            self.assertEqual(cfg["fusion"]["thresholds_preset"], "balanced")
            self.assertAlmostEqual(float(cfg["fusion"]["thresholds"]["acquire_threshold"]), 0.20, places=6)
            self.assertEqual(int(cfg["fusion"]["thresholds"]["hold_ms"]), 1500)
            self.assertEqual(int(cfg["fusion"]["thresholds"]["min_switch_interval_ms"]), 500)


if __name__ == "__main__":
    unittest.main()
