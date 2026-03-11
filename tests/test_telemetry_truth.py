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
                "active_thresholds": {"acquire": 0.58, "drop": 0.32},
            },
            "faces": [],
            "candidates": [],
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
        self.assertEqual(snapshot["bus_drop_counts_window"], {"audio.frames": 2})
        self.assertEqual(snapshot["capture_overflow_window"], 3)
        self.assertEqual(snapshot["runtime_profile"], "realtime_pi_max")
        self.assertTrue(snapshot["strict_requirements_passed"])


if __name__ == "__main__":
    unittest.main()
