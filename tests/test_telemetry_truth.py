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
                "target_camera_id": "cam0",
                "focus_score": 0.91,
                "activity_score": 0.72,
                "selection_mode": "AV_LOCK",
                "score_margin": 0.23,
                "runner_up_focus_score": 0.68,
                "active_thresholds": {"acquire": 0.58, "drop": 0.32},
                "timing_window_ms": {"hold_ms": 800, "handoff_min_ms": 700},
                "evidence_status": {"visual_fresh": True, "audio_fresh": True, "disagreement_suppressed": False},
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
                    "camera_id": "cam0",
                    "bearing_deg": 90.0,
                    "focus_score": 0.91,
                    "activity_score": 0.72,
                    "selection_mode": "AV_LOCK",
                    "score_components": {"visual_speaking_prob": 0.71, "doa_peak_score": 0.7, "audio_speech_prob": 0.6},
                    "score_groups": {"visual_score": 0.76, "audio_alignment_score": 0.65, "disagreement_penalty": 0.0},
                    "evidence_status": {"visual_fresh": True, "audio_fresh": True, "disagreement_suppressed": False},
                    "speaking": True,
                },
                {
                    "track_id": "cam1-2",
                    "camera_id": "cam1",
                    "bearing_deg": 180.0,
                    "focus_score": 0.68,
                    "activity_score": 0.33,
                    "selection_mode": "VISION_ONLY",
                    "score_components": {"visual_speaking_prob": 0.31, "doa_peak_score": 0.2, "audio_speech_prob": 0.1},
                    "score_groups": {"visual_score": 0.31, "audio_alignment_score": 0.22, "disagreement_penalty": 0.1},
                    "evidence_status": {"visual_fresh": True, "audio_fresh": True, "disagreement_suppressed": True},
                    "speaking": False,
                },
            ],
            "candidates_evidence": {
                "reason": "faces_and_audio",
                "faces_present": True,
                "faces_fresh": True,
                "audio_fresh": True,
                "audio_stale": False,
                "visual_stale": False,
                "disagreement_suppressed": True,
            },
            "output": {"sink": "usb_mic", "underrun_rate": 0.01, "buffer_occupancy": 0.52},
            "vision_debug": {"detector_backend": "haar", "detector_degraded": {"active": False}},
            "perf": {"bus_drop_counts_window": {"audio.frames": 2}},
            "runtime_profile": "realtime_pi_max",
            "strict_requirements_passed": True,
            "detector_backend_active": "haar",
            "overflow_window": 3,
            "runtime_cfg": {
                "selected_audio_device": {"device_index": 3, "device_name": "External Microphone", "channels": 1},
                "audio_yaw_offset_deg": 15.0,
                "audio_yaw_calibration": {
                    "profile_yaw_offset_deg": 7.5,
                    "base_runtime_yaw_offset_deg": 15.0,
                    "sidecar_yaw_offset_deg": 2.5,
                    "effective_runtime_yaw_offset_deg": 17.5,
                    "effective_total_yaw_offset_deg": 25.0,
                },
                "audio_calibration_overlay": {
                    "active": True,
                    "status": "active",
                    "source": "sidecar",
                    "reload_behavior": "startup_only",
                    "restart_required_on_change": True,
                    "hot_reload_supported": False,
                },
                "camera_calibration_overlay": {"active": True, "status": "active"},
            },
            "configured_camera_map": [{"id": "cam0", "yaw_offset_deg": 90.0, "hfov_deg": 160.0}],
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
        self.assertEqual(snapshot["lock_state"]["target_camera_id"], "cam0")
        self.assertEqual(snapshot["lock_state"]["active_thresholds"]["acquire"], 0.58)
        self.assertEqual(snapshot["top_candidates"][0]["focus_score"], 0.91)
        self.assertEqual(snapshot["top_candidates"][0]["camera_id"], "cam0")
        self.assertEqual(snapshot["top_candidates"][0]["score_groups"]["visual_score"], 0.76)
        self.assertEqual(snapshot["output_summary"]["sink"], "usb_mic")
        self.assertEqual(snapshot["audio_route_summary"]["input_device_name"], "External Microphone")
        self.assertEqual(snapshot["audio_route_summary"]["output_sink"], "usb_mic")
        self.assertFalse(snapshot["audio_route_summary"]["input_loopback_risk"])
        self.assertEqual(snapshot["bus_drop_counts_window"], {"audio.frames": 2})
        self.assertEqual(snapshot["capture_overflow_window"], 3)
        self.assertEqual(snapshot["runtime_profile"], "realtime_pi_max")
        self.assertTrue(snapshot["strict_requirements_passed"])
        self.assertTrue(snapshot["fusion_debug"]["disagreement_suppressed"])
        self.assertTrue(snapshot["meta"]["audio_vad_enabled"])
        self.assertEqual(snapshot["meta"]["runtime_config"]["audio_yaw_offset_deg"], 15.0)
        self.assertEqual(snapshot["meta"]["audio_calibration_overlay"]["reload_behavior"], "startup_only")
        self.assertTrue(snapshot["meta"]["audio_calibration_overlay"]["restart_required_on_change"])
        self.assertEqual(snapshot["meta"]["runtime_config"]["audio_yaw_calibration"]["profile_yaw_offset_deg"], 7.5)
        self.assertEqual(snapshot["meta"]["runtime_config"]["audio_yaw_calibration"]["sidecar_yaw_offset_deg"], 2.5)
        self.assertEqual(snapshot["meta"]["runtime_config"]["audio_yaw_calibration"]["effective_total_yaw_offset_deg"], 25.0)

    def test_snapshot_flags_blackhole_output_and_loopback_input_risk(self) -> None:
        snapshot = _build_snapshot(
            {
                "audio_heatmap": {},
                "faces": [],
                "lock": {},
                "output": {
                    "sink": "host_loopback",
                    "device_name": "BlackHole 2ch",
                    "underrun_window": 1,
                    "underrun_total": 2,
                    "device_error_total": 0,
                },
                "runtime_cfg": {
                    "selected_audio_device": {
                        "device_index": 1,
                        "device_name": "BlackHole 2ch",
                        "channels": 2,
                    }
                },
                "logs": [],
            },
            4,
        )

        route = snapshot["audio_route_summary"]
        self.assertEqual(route["output_device_name"], "BlackHole 2ch")
        self.assertTrue(route["output_blackhole_active"])
        self.assertTrue(route["input_loopback_risk"])
        self.assertEqual(route["output_underrun_window"], 1)


if __name__ == "__main__":
    unittest.main()
