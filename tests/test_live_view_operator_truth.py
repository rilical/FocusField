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

    def test_live_page_marks_stale_camera_frames_from_health_topics(self) -> None:
        self.assertIn('id="cam1-status"', self.html)
        self.assertIn("const CAMERA_STALE_MS = 1500;", self.html)
        self.assertIn("cameraFrameHealth(data, camId)", self.html)
        self.assertIn("topics['vision.frames.' + camId]", self.html)
        self.assertIn("tile.classList.toggle('camera-stale', isStale)", self.html)
        self.assertIn("NO SIGNAL", self.html)

    def test_live_page_has_responsive_operator_dashboard_breakpoints(self) -> None:
        self.assertIn("@media (max-width: 900px)", self.html)
        self.assertIn("@media (max-width: 700px)", self.html)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", self.html)
        self.assertIn("#audio-direction-row", self.html)
        self.assertIn("width: min(100%, 276px)", self.html)
        self.assertIn("grid-row: auto !important", self.html)
        self.assertIn("grid-column: 1 / 3", self.html)
