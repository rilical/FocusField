import unittest

from focusfield.ui.telemetry import _build_snapshot  # noqa: PLC2701


class TelemetryTruthTests(unittest.TestCase):
    def test_snapshot_prefers_audio_heatmap_for_primary_summary(self) -> None:
        state = {
            "audio_heatmap": {
                "bins": 4,
                "bin_size_deg": 90.0,
                "confidence": 0.8,
                "peaks": [{"angle_deg": 90.0, "score": 0.7}],
                "heatmap": [0.0, 0.7, 0.2, 0.1],
            },
            "heatmap": {
                "bins": 4,
                "bin_size_deg": 90.0,
                "confidence": 0.2,
                "peaks": [{"angle_deg": 180.0, "score": 0.1}],
                "heatmap": [0.1, 0.0, 0.0, 0.0],
            },
            "lock": {
                "state": "LOCKED",
                "mode": "AV_LOCK",
                "target_id": "cam0-1",
                "focus_score": 0.91,
                "activity_score": 0.72,
                "selection_mode": "AV_LOCK",
                "score_margin": 0.23,
                "runner_up_focus_score": 0.68,
                "active_thresholds": {"acquire": 0.58, "drop": 0.32},
            },
            "faces": [
                {
                    "track_id": "cam0-1",
                    "bearing_deg": 90.0,
                    "mouth_activity": 0.63,
                    "visual_speaking_prob": 0.71,
                    "visual_quality": 0.88,
                    "motion_activity": 0.55,
                    "landmark_presence": 1.0,
                    "speaking": True,
                }
            ],
            "candidates": [
                {
                    "track_id": "cam0-1",
                    "bearing_deg": 90.0,
                    "focus_score": 0.91,
                    "activity_score": 0.72,
                    "selection_mode": "AV_LOCK",
                    "score_components": {"visual_speaking_prob": 0.71, "doa_peak_score": 0.7, "audio_speech_prob": 0.6},
                    "speaking": True,
                },
                {
                    "track_id": "cam1-2",
                    "bearing_deg": 180.0,
                    "focus_score": 0.68,
                    "activity_score": 0.33,
                    "selection_mode": "VISION_ONLY",
                    "score_components": {"visual_speaking_prob": 0.31, "doa_peak_score": 0.2, "audio_speech_prob": 0.1},
                    "speaking": False,
                },
            ],
            "output": {"sink": "usb_mic", "underrun_rate": 0.01, "buffer_occupancy": 0.52},
            "vision_debug": {"detector_backend": "haar", "detector_degraded": {"active": False}},
            "perf": {"bus_drop_counts_window": {"audio.frames": 2}},
            "runtime_profile": "realtime_pi_max",
            "strict_requirements_passed": True,
            "detector_backend_active": "haar",
            "overflow_window": 3,
            "logs": [],
        }
        snapshot = _build_snapshot(state, 1)
        self.assertEqual(snapshot["heatmap_summary"]["confidence"], 0.8)
        self.assertEqual(snapshot["vision_heatmap_summary"]["confidence"], 0.2)
        self.assertEqual(snapshot["fusion_debug"]["doa_confidence"], 0.8)
        self.assertEqual(snapshot["fusion_debug"]["doa_peak_score"], 0.7)
        self.assertEqual(snapshot["lock_state"]["focus_score"], 0.91)
        self.assertEqual(snapshot["lock_state"]["activity_score"], 0.72)
        self.assertEqual(snapshot["lock_state"]["selection_mode"], "AV_LOCK")
        self.assertEqual(snapshot["lock_state"]["runner_up_focus_score"], 0.68)
        self.assertEqual(snapshot["top_candidates"][0]["focus_score"], 0.91)
        self.assertEqual(snapshot["output_summary"]["sink"], "usb_mic")
        self.assertEqual(snapshot["bus_drop_counts_window"], {"audio.frames": 2})
        self.assertEqual(snapshot["capture_overflow_window"], 3)
        self.assertEqual(snapshot["runtime_profile"], "realtime_pi_max")
        self.assertTrue(snapshot["strict_requirements_passed"])


if __name__ == "__main__":
    unittest.main()
