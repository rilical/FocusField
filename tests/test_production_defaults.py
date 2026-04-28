import unittest
import tempfile
from pathlib import Path

from focusfield.core.config import load_config


class ProductionDefaultTests(unittest.TestCase):
    def test_meeting_peripheral_config_is_explicit_three_camera_eight_mic_profile(self) -> None:
        cfg = load_config("configs/meeting_peripheral.yaml")
        self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
        self.assertEqual(cfg["runtime"]["perf_profile"], "realtime_pi_max")
        self.assertEqual(cfg["audio"]["channels"], 8)
        self.assertEqual(cfg["audio"]["device_profile"], "minidsp_uma8_raw_7p1")
        self.assertEqual(len(cfg["video"]["cameras"]), 3)
        self.assertEqual(cfg["output"]["sink"], "usb_mic")
        self.assertFalse(bool(cfg["runtime"]["fail_fast"]))
        self.assertEqual(int(cfg["audio"]["block_size"]), 2048)
        self.assertFalse(bool(cfg["audio"]["mic_health"]["enabled"]))
        self.assertTrue(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(str(cfg["audio"]["vad"]["backend"]), "webrtc")
        self.assertEqual(int(cfg["audio"]["doa"]["bins"]), 36)
        self.assertEqual(int(cfg["audio"]["doa"]["update_hz"]), 4)
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertEqual(int(cfg["video"]["cameras"][0]["fps"]), 6)
        self.assertEqual(int(cfg["vision"]["face"]["detect_every_n"]), 2)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["weights"]["mic_health"]), 0.0)
        self.assertEqual(float(cfg["fusion"]["visual_freshness_ms"]), 1200.0)
        self.assertEqual(float(cfg["fusion"]["visual_override_min"]), 0.6)
        self.assertEqual(float(cfg["fusion"]["audio_rescue_min"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["disagreement_penalty"]), 0.25)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.frames"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertFalse(bool(cfg["uma8_leds"]["enabled"]))
        self.assertFalse(bool(cfg["ui"]["enabled"]))

    def test_demo_safe_profile_prefers_stability_and_evidence_collection(self) -> None:
        cfg = load_config("configs/meeting_peripheral_demo_safe.yaml")
        self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
        self.assertTrue(bool(cfg["trace"]["enabled"]))
        self.assertEqual(int(cfg["audio"]["block_size"]), 2048)
        self.assertFalse(bool(cfg["audio"]["mic_health"]["enabled"]))
        self.assertTrue(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(str(cfg["audio"]["vad"]["backend"]), "webrtc")
        self.assertEqual(int(cfg["audio"]["doa"]["bins"]), 36)
        self.assertEqual(int(cfg["audio"]["doa"]["update_hz"]), 4)
        self.assertFalse(bool(cfg["fusion"]["require_vad"]))
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertEqual(cfg["fusion"]["thresholds_preset"], "")
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["visual_freshness_ms"]), 1200.0)
        self.assertEqual(float(cfg["fusion"]["visual_override_min"]), 0.6)
        self.assertEqual(float(cfg["fusion"]["audio_rescue_min"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["disagreement_penalty"]), 0.25)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["acquire_threshold"]), 0.24)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["hold_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["handoff_min_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["min_switch_interval_ms"]), 1800)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["speak_on_threshold"]), 0.14)
        self.assertEqual(int(cfg["video"]["cameras"][0]["fps"]), 6)
        self.assertEqual(int(cfg["vision"]["face"]["detect_every_n"]), 2)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.frames"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertFalse(bool(cfg["ui"]["enabled"]))

    def test_mode_defaults_override_generic_defaults_for_minimal_mode_only_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("runtime:\n  mode: meeting_peripheral\n", encoding="utf-8")
            cfg = load_config(str(root / "config.yaml"))
            self.assertEqual(cfg["runtime"]["mode"], "meeting_peripheral")
            self.assertFalse(bool(cfg["trace"]["enabled"]))
            self.assertFalse(bool(cfg["ui"]["enabled"]))
            self.assertEqual(str(cfg["output"]["sink"]), "usb_mic")

    def test_pi_demo_live_profile_is_trimmed_for_single_speaker_stability(self) -> None:
        cfg = load_config("configs/full_3cam_8mic_pi_demo_live.yaml")
        self.assertFalse(bool(cfg["trace"]["enabled"]))
        self.assertFalse(bool(cfg["ui"]["enabled"]))
        self.assertEqual(str(cfg["runtime"]["process_mode"]), "multiprocess")
        self.assertTrue(bool(cfg["runtime"]["realtime"]["enabled"]))
        self.assertTrue(bool(cfg["runtime"]["startup"]["audio_first"]))
        self.assertTrue(bool(cfg["runtime"]["startup"]["overload_shed_enabled"]))
        self.assertTrue(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(str(cfg["audio"]["vad"]["backend"]), "webrtc")
        self.assertFalse(bool(cfg["fusion"]["require_vad"]))
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(float(cfg["fusion"]["visual_override_min"]), 0.5)
        self.assertEqual(float(cfg["fusion"]["visual_freshness_ms"]), 1800)
        self.assertEqual(float(cfg["fusion"]["disagreement_penalty"]), 0.18)
        self.assertEqual(int(cfg["audio"]["block_size"]), 2048)
        self.assertEqual(int(cfg["audio"]["capture"]["queue_depth"]), 64)
        self.assertEqual(int(cfg["audio"]["vad"]["update_hz"]), 4)
        self.assertEqual(int(cfg["audio"]["doa"]["bins"]), 36)
        self.assertEqual(int(cfg["audio"]["doa"]["update_hz"]), 4)
        self.assertEqual(int(cfg["video"]["cameras"][0]["fps"]), 5)
        self.assertTrue(bool(cfg["video"]["camera_controls"]["enabled"]))
        self.assertEqual(int(cfg["video"]["camera_controls"]["defaults"]["auto_exposure"]), 1)
        self.assertEqual(int(cfg["video"]["camera_controls"]["defaults"]["exposure_time_absolute"]), 24)
        self.assertEqual(int(cfg["vision"]["face"]["detect_every_n"]), 2)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["acquire_threshold"]), 0.24)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["hold_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["handoff_min_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["min_switch_interval_ms"]), 1800)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["speak_on_threshold"]), 0.14)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.vad"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertEqual(str(cfg["bus"]["topic_queue_policies"]["audio.frames"]), "drop_oldest")
        self.assertEqual(str(cfg["output"]["sink"]), "none")
        self.assertFalse(bool(cfg["uma8_leds"]["enabled"]))

    def test_pi_demo_observe_profile_keeps_low_rate_ui_without_trace(self) -> None:
        cfg = load_config("configs/full_3cam_8mic_pi_demo_observe.yaml")
        self.assertFalse(bool(cfg["trace"]["enabled"]))
        self.assertTrue(bool(cfg["ui"]["enabled"]))
        self.assertEqual(str(cfg["runtime"]["process_mode"]), "multiprocess")
        self.assertTrue(bool(cfg["runtime"]["realtime"]["enabled"]))
        self.assertTrue(bool(cfg["runtime"]["startup"]["audio_first"]))
        self.assertTrue(bool(cfg["runtime"]["startup"]["overload_shed_enabled"]))
        self.assertTrue(bool(cfg["audio"]["vad"]["enabled"]))
        self.assertEqual(str(cfg["audio"]["vad"]["backend"]), "webrtc")
        self.assertEqual(int(cfg["ui"]["telemetry_hz"]), 1)
        self.assertEqual(int(cfg["ui"]["frame_max_hz"]), 1)
        self.assertFalse(bool(cfg["fusion"]["require_vad"]))
        self.assertTrue(bool(cfg["fusion"]["require_speaking"]))
        self.assertFalse(bool(cfg["fusion"]["audio_fallback"]["enabled"]))
        self.assertEqual(float(cfg["fusion"]["visual_override_min"]), 0.5)
        self.assertEqual(float(cfg["fusion"]["visual_freshness_ms"]), 1800)
        self.assertEqual(float(cfg["fusion"]["disagreement_penalty"]), 0.18)
        self.assertEqual(int(cfg["audio"]["vad"]["update_hz"]), 4)
        self.assertEqual(int(cfg["audio"]["doa"]["bins"]), 36)
        self.assertEqual(int(cfg["audio"]["doa"]["update_hz"]), 4)
        self.assertEqual(str(cfg["vision"]["mouth"]["backend"]), "diff")
        self.assertEqual(float(cfg["fusion"]["weights"]["audio"]), 0.35)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["acquire_threshold"]), 0.24)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["hold_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["handoff_min_ms"]), 2200)
        self.assertEqual(int(cfg["fusion"]["thresholds"]["min_switch_interval_ms"]), 1800)
        self.assertEqual(float(cfg["fusion"]["thresholds"]["speak_on_threshold"]), 0.14)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["audio.vad"]), 16)
        self.assertEqual(int(cfg["bus"]["topic_queue_depths"]["fusion.target_lock"]), 8)
        self.assertEqual(int(cfg["audio"]["capture"]["queue_depth"]), 64)
        self.assertEqual(str(cfg["bus"]["topic_queue_policies"]["audio.frames"]), "drop_oldest")
        self.assertTrue(bool(cfg["video"]["camera_controls"]["enabled"]))
        self.assertEqual(int(cfg["video"]["camera_controls"]["defaults"]["white_balance_automatic"]), 0)

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
