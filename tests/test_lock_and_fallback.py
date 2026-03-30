import unittest

from focusfield.core.clock import now_ns
from focusfield.fusion.lock_state_machine import LockStateMachine
from focusfield.fusion.av_association import _build_audio_only_candidate, _build_candidates  # noqa: PLC2701


class LockStateMachineTests(unittest.TestCase):
    def test_acquire_then_locked(self) -> None:
        config = {
            "fusion": {
                "thresholds": {
                    "acquire_threshold": 0.65,
                    "acquire_timeout_ms": 500,
                    "hold_ms": 800,
                    "handoff_min_ms": 700,
                    "speak_on_threshold": 0.5,
                    "min_switch_interval_ms": 0,
                    "bearing_smoothing_alpha": 1.0,
                },
                "require_speaking": True,
                "require_vad": False,
            }
        }
        machine = LockStateMachine(config)

        low = [
            {
                "track_id": "cam0-1",
                "bearing_deg": 10.0,
                "combined_score": 0.50,
                "speaking": True,
                "doa_peak_deg": None,
                "score_components": {"mouth_activity": 0.6},
            }
        ]
        msg1 = machine.update(low, vad_state=None)
        self.assertEqual(msg1["state"], "ACQUIRE")
        self.assertEqual(msg1["reason"], "acquire_start")

        high = [
            {
                "track_id": "cam0-1",
                "bearing_deg": 12.0,
                "combined_score": 0.80,
                "speaking": True,
                "doa_peak_deg": None,
                "score_components": {"mouth_activity": 0.8},
            }
        ]
        msg2 = machine.update(high, vad_state=None)
        self.assertEqual(msg2["state"], "LOCKED")
        self.assertEqual(msg2["reason"], "acquired")

    def test_audio_only_mode(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40}}}
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "audio:peak0",
                "bearing_deg": 123.0,
                "combined_score": 0.9,
                "speaking": True,
                "doa_peak_deg": 123.0,
                "score_components": {"doa_peak_score": 1.0},
            }
        ]
        msg = machine.update(cand, vad_state={"t_ns": msg_t_ns_placeholder(), "speech": True})
        self.assertEqual(msg["state"], "LOCKED")
        self.assertEqual(msg["mode"], "AUDIO_ONLY")
        self.assertIsNotNone(msg["target_bearing_deg"])

    def test_av_lock_mode(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40}}}
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam0-1",
                "bearing_deg": 200.0,
                "combined_score": 0.9,
                "activity_score": 1.0,
                "speaking_probability": 1.0,
                "speaking": True,
                "doa_peak_deg": 205.0,
                "score_components": {"mouth_activity": 1.0, "doa_peak_score": 1.0},
            }
        ]
        msg = machine.update(cand, vad_state=None)
        self.assertEqual(msg["state"], "LOCKED")
        self.assertEqual(msg["mode"], "AV_LOCK")
        self.assertEqual(msg["focus_score"], 0.9)
        self.assertEqual(msg["activity_score"], 1.0)
        self.assertEqual(msg["selection_mode"], "AV_LOCK")

    def test_focus_score_beats_combined_score_alias(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40, "min_switch_interval_ms": 0}}}
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam0-1",
                "bearing_deg": 25.0,
                "focus_score": 0.87,
                "combined_score": 0.12,
                "activity_score": 0.62,
                "speaking_probability": 0.62,
                "speaking": True,
                "doa_peak_deg": 25.0,
                "score_components": {"visual_speaking_prob": 0.62, "doa_peak_score": 0.8},
            }
        ]
        msg = machine.update(cand, vad_state=None)
        self.assertEqual(msg["state"], "LOCKED")
        self.assertAlmostEqual(msg["focus_score"], 0.87, places=6)
        self.assertAlmostEqual(msg["confidence"], 0.87, places=6)
        self.assertAlmostEqual(msg["activity_score"], 0.62, places=6)

    def test_acquire_persist_can_commit_stable_single_face_below_primary_threshold(self) -> None:
        config = {
            "fusion": {
                "thresholds": {
                    "acquire_threshold": 0.50,
                    "acquire_persist_ms": 100,
                    "acquire_floor_ratio": 0.50,
                    "min_switch_interval_ms": 0,
                },
                "require_speaking": False,
                "require_vad": False,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam2-1",
                "bearing_deg": 243.0,
                "focus_score": 0.30,
                "activity_score": 0.20,
                "speaking_probability": 0.20,
                "speaking": False,
                "score_components": {"visual_speaking_prob": 0.20},
            }
        ]
        msg1 = machine.update(cand, vad_state=None)
        self.assertEqual(msg1["state"], "ACQUIRE")
        machine._acquire_start_ns = now_ns() - 200_000_000
        msg2 = machine.update(cand, vad_state=None)
        self.assertEqual(msg2["state"], "LOCKED")
        self.assertEqual(msg2["reason"], "acquired_persist")
        self.assertEqual(msg2["mode"], "VISION_ONLY")


class AvAssociationSizingTests(unittest.TestCase):
    def test_size_scale_downweights_small_faces(self) -> None:
        weights = {"mouth": 0.7, "face": 0.3, "doa": 0.0, "angle": 0.0}
        tracks = [
            {
                "seq": 1,
                "track_id": "big",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "confidence": 1.0,
                "speaking": True,
                "bbox": {"x": 0, "y": 0, "w": 100, "h": 100},  # area 10000 => scale 1
            },
            {
                "seq": 1,
                "track_id": "small",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "confidence": 1.0,
                "speaking": True,
                "bbox": {"x": 0, "y": 0, "w": 30, "h": 30},  # area 900 => scale 0
            },
        ]
        cands = _build_candidates(tracks, doa_heatmap=None, max_assoc_deg=20.0, weights=weights, min_area=900, area_soft_max=3600)
        by_id = {c["track_id"]: c for c in cands}
        self.assertGreater(float(by_id["big"]["combined_score"]), float(by_id["small"]["combined_score"]))
        self.assertEqual(float(by_id["small"]["combined_score"]), 0.0)

    def test_audio_only_candidate_requires_fresh_vad_for_speaking(self) -> None:
        now = now_ns()
        doa_heatmap = {"seq": 1, "confidence": 0.9, "peaks": [{"angle_deg": 15.0, "score": 0.8}]}
        stale = {"speech": True, "confidence": 1.0, "t_ns": now - 2_000_000_000}
        stale_cand = _build_audio_only_candidate(
            doa_heatmap,
            stale,
            mic_health=None,
            min_doa_confidence=0.45,
            min_peak_score=0.30,
            score_mode="confidence",
            require_vad=False,
            weights={},
            vad_max_age_ms=400.0,
        )
        self.assertIsNotNone(stale_cand)
        self.assertFalse(bool(stale_cand["speaking"]))

        fresh = {"speech": True, "confidence": 1.0, "t_ns": now}
        fresh_cand = _build_audio_only_candidate(
            doa_heatmap,
            fresh,
            mic_health=None,
            min_doa_confidence=0.45,
            min_peak_score=0.30,
            score_mode="confidence",
            require_vad=True,
            weights={},
            vad_max_age_ms=400.0,
        )
        self.assertIsNotNone(fresh_cand)
        self.assertTrue(bool(fresh_cand["speaking"]))


def msg_t_ns_placeholder() -> int:
    # LockStateMachine only needs a numeric `t_ns` for freshness checks. Tests don't
    # depend on actual monotonic time.
    return 1


if __name__ == "__main__":
    unittest.main()
