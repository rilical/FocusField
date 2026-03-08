import unittest

import numpy as np

from focusfield.audio.mic_health import MicHealthAnalyzer
from focusfield.fusion.av_association import _build_candidates  # noqa: PLC2701
from focusfield.fusion.speaker_posterior import estimate_speaker_posterior


class MicHealthTests(unittest.TestCase):
    def test_dead_and_clipping_channels_are_downweighted(self) -> None:
        analyzer = MicHealthAnalyzer({"audio": {"mic_health": {"enabled": True}}})
        frame = np.zeros((512, 4), dtype=np.float32)
        frame[:, 1] = 1.0
        frame[:, 2] = 0.02 * np.random.randn(512).astype(np.float32)
        frame[:, 3] = 0.02 * np.random.randn(512).astype(np.float32)
        msg = analyzer.update({"t_ns": 1, "seq": 1, "data": frame})
        self.assertIsNotNone(msg)
        channels = {entry["channel"]: entry for entry in msg["channels"]}
        self.assertLess(float(channels[0]["score"]), 0.2)
        self.assertIn("dead", str(channels[0]["bad_reason"]))
        self.assertLess(float(channels[1]["score"]), 0.3)
        self.assertIn("clipping", str(channels[1]["bad_reason"]))


class SpeakerPosteriorTests(unittest.TestCase):
    def test_posterior_prefers_multimodal_agreement(self) -> None:
        strong = estimate_speaker_posterior(
            visual_speaking_prob=0.9,
            face_confidence=0.8,
            doa_peak_score=0.85,
            doa_confidence=0.8,
            angle_error_deg=4.0,
            audio_speech_prob=0.9,
            track_continuity=0.8,
            mic_health_score=0.9,
            weights={},
        )
        weak = estimate_speaker_posterior(
            visual_speaking_prob=0.1,
            face_confidence=0.2,
            doa_peak_score=0.15,
            doa_confidence=0.2,
            angle_error_deg=80.0,
            audio_speech_prob=0.1,
            track_continuity=0.1,
            mic_health_score=0.9,
            weights={},
        )
        self.assertGreater(strong, weak)
        self.assertGreater(strong, 0.7)
        self.assertLess(weak, 0.2)

    def test_build_candidates_keeps_small_face_at_zero_without_audio_or_doa(self) -> None:
        weights = {"mouth": 1.05, "face": 0.35, "doa": 1.15, "angle": 0.95}
        tracks = [
            {
                "seq": 1,
                "track_id": "small",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "visual_speaking_prob": 1.0,
                "confidence": 1.0,
                "speaking": True,
                "track_age_frames": 2,
                "bbox": {"x": 0, "y": 0, "w": 30, "h": 30},
            }
        ]
        cands = _build_candidates(
            tracks=tracks,
            doa_heatmap=None,
            vad_state=None,
            mic_health=None,
            max_assoc_deg=20.0,
            weights=weights,
            min_area=900,
            area_soft_max=3600,
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(float(cands[0]["combined_score"]), 0.0)


if __name__ == "__main__":
    unittest.main()
