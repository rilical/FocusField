import unittest

from focusfield.ui.views.live import live_page


class LiveViewOperatorTruthContractTests(unittest.TestCase):
    """Contract tests for the operator-truth UI surface in live view."""

    def setUp(self) -> None:
        self.html = live_page()

    def test_live_page_exposes_operator_truth_dom_nodes(self) -> None:
        self.assertIn('id="calibration-status"', self.html)
        self.assertIn('id="audio-calibration-status"', self.html)
        self.assertIn('id="audio-yaw-breakdown"', self.html)
        self.assertIn('id="lock-reason-value"', self.html)
        self.assertIn('id="alignment-target-bearing"', self.html)
        self.assertIn('id="alignment-beam-bearing"', self.html)
        self.assertIn('id="alignment-doa-peak"', self.html)
        self.assertIn('id="alignment-camera"', self.html)
        self.assertIn('id="alignment-disagreement"', self.html)
        self.assertIn('id="audio-dir-peak"', self.html)
        self.assertIn('id="audio-dir-beam"', self.html)
        self.assertIn('id="audio-dir-led"', self.html)

    def test_live_page_updates_operator_truth_from_existing_telemetry_fields(self) -> None:
        self.assertIn("lock.reason", self.html)
        self.assertIn("lockState.target_camera_id", self.html)
        self.assertIn("meta.camera_calibration_overlay", self.html)
        self.assertIn("meta.audio_calibration_overlay", self.html)
        self.assertIn("calibration.status", self.html)
        self.assertIn("audioCalibration.status", self.html)
        self.assertIn("audio_yaw_calibration", self.html)
        self.assertIn("findPeakDoa(heatmapSummary)", self.html)
        self.assertIn("beamformer.target_bearing_deg", self.html)
        self.assertIn("top_candidates", self.html)
        self.assertIn("updateOperatorTruth", self.html)
        self.assertIn("meta.audio_vad_enabled", self.html)
        self.assertIn("VAD: off", self.html)
        self.assertIn("restart to apply changes", self.html)

    def test_live_page_does_not_report_target_camera_when_state_is_no_lock(self) -> None:
        self.assertIn("if (state === 'NO_LOCK')", self.html)
        self.assertIn("return '—';", self.html)
