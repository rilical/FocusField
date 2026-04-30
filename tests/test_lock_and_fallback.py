import unittest
import threading
import queue
import time

from focusfield.core.bus import Bus
from focusfield.core.clock import now_ns
from focusfield.fusion.lock_state_machine import LockStateMachine
from focusfield.fusion.av_association import _build_audio_only_candidate, _build_candidates, start_av_association  # noqa: PLC2701


class _NoopLogger:
    def emit(self, *_args, **_kwargs) -> None:
        return None


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
        self.assertEqual(msg2["active_thresholds"]["acquire"], 0.65)
        self.assertEqual(msg2["timing_window_ms"]["hold_ms"], 800.0)

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
        msg = machine.update(cand, vad_state={"t_ns": now_ns(), "speech": True})
        self.assertEqual(msg["state"], "LOCKED")
        self.assertEqual(msg["mode"], "AUDIO_ONLY")
        self.assertIsNotNone(msg["target_bearing_deg"])

    def test_audio_only_target_drops_when_fresh_visual_has_no_candidate(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40, "hold_ms": 800}}}
        machine = LockStateMachine(config)
        acquired = machine.update(
            [
                {
                    "track_id": "audio:peak0",
                    "bearing_deg": 123.0,
                    "combined_score": 0.9,
                    "speaking": True,
                    "doa_peak_deg": 123.0,
                    "score_components": {"doa_peak_score": 1.0},
                }
            ],
            vad_state={"t_ns": now_ns(), "speech": True},
        )
        self.assertEqual(acquired["state"], "LOCKED")
        self.assertEqual(acquired["mode"], "AUDIO_ONLY")

        dropped = machine.update(
            {
                "candidates": [],
                "evidence": {
                    "reason": "recent_faces_unassociated",
                    "faces_present": True,
                    "faces_fresh": True,
                    "audio_fresh": True,
                },
            },
            vad_state={"t_ns": now_ns(), "speech": True},
        )
        self.assertEqual(dropped["state"], "NO_LOCK")
        self.assertEqual(dropped["reason"], "audio_only_suppressed_by_fresh_visual")
        self.assertIsNone(dropped["target_id"])

    def test_vad_speech_does_not_acquire_silent_visual_when_visual_speaking_required(self) -> None:
        config = {
            "fusion": {
                "thresholds": {"acquire_threshold": 0.40, "speak_on_threshold": 0.14},
                "require_speaking": True,
                "require_vad": False,
                "require_visual_speaking_for_visual_lock": True,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam1-4",
                "camera_id": "cam1",
                "bearing_deg": 130.0,
                "focus_score": 0.88,
                "activity_score": 0.02,
                "speaking_probability": 0.02,
                "speaking": False,
                "score_components": {"visual_speaking_prob": 0.02},
            }
        ]
        msg = machine.update(cand, vad_state={"t_ns": now_ns(), "speech": True})
        self.assertEqual(msg["state"], "NO_LOCK")
        self.assertNotEqual(msg["target_id"], "cam1-4")

    def test_visual_speaking_gate_ignores_probability_spike_without_tracker_speaking(self) -> None:
        config = {
            "fusion": {
                "thresholds": {"acquire_threshold": 0.40, "speak_on_threshold": 0.14},
                "require_speaking": True,
                "require_vad": False,
                "require_visual_speaking_for_visual_lock": True,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam1-3",
                "camera_id": "cam1",
                "bearing_deg": 130.0,
                "focus_score": 0.59,
                "activity_score": 0.37,
                "speaking_probability": 0.37,
                "speaking": False,
                "score_components": {"visual_speaking_prob": 0.37},
            }
        ]
        msg = machine.update(cand, vad_state=None)
        self.assertEqual(msg["state"], "NO_LOCK")
        self.assertEqual(msg["reason"], "silence_drop")
        self.assertIsNone(msg["target_id"])

    def test_fresh_vad_and_raw_mouth_motion_can_acquire_visual_target(self) -> None:
        config = {
            "fusion": {
                "thresholds": {"acquire_threshold": 0.40, "speak_on_threshold": 0.14, "min_switch_interval_ms": 0},
                "require_speaking": True,
                "require_vad": False,
                "require_visual_speaking_for_visual_lock": True,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam1-9",
                "camera_id": "cam1",
                "bearing_deg": 116.0,
                "focus_score": 0.59,
                "activity_score": 0.31,
                "speaking_probability": 0.31,
                "speaking": False,
                "score_components": {
                    "explicit_mouth_activity": 0.18,
                    "mouth_activity": 0.18,
                    "visual_speaking_prob": 0.27,
                    "audio_speech_prob": 1.0,
                },
            }
        ]
        msg = machine.update(cand, vad_state={"t_ns": now_ns(), "speech": True})
        self.assertEqual(msg["state"], "LOCKED")
        self.assertEqual(msg["target_id"], "cam1-9")
        self.assertEqual(msg["target_camera_id"], "cam1")

    def test_raw_mouth_motion_requires_vad_when_visual_gate_is_explicit(self) -> None:
        config = {
            "fusion": {
                "thresholds": {"acquire_threshold": 0.40, "speak_on_threshold": 0.14},
                "require_speaking": True,
                "require_vad": False,
                "require_visual_speaking_for_visual_lock": True,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam1-9",
                "camera_id": "cam1",
                "bearing_deg": 116.0,
                "focus_score": 0.59,
                "activity_score": 0.31,
                "speaking_probability": 0.31,
                "speaking": False,
                "score_components": {
                    "explicit_mouth_activity": 0.18,
                    "mouth_activity": 0.18,
                    "visual_speaking_prob": 0.27,
                    "audio_speech_prob": 0.0,
                },
            }
        ]
        msg = machine.update(cand, vad_state={"t_ns": now_ns(), "speech": False})
        self.assertEqual(msg["state"], "NO_LOCK")
        self.assertEqual(msg["reason"], "silence_drop")
        self.assertIsNone(msg["target_id"])

    def test_vad_speech_can_still_acquire_audio_only_when_visual_speaking_required(self) -> None:
        config = {
            "fusion": {
                "thresholds": {"acquire_threshold": 0.40},
                "require_speaking": True,
                "require_vad": False,
                "require_visual_speaking_for_visual_lock": True,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "audio:peak0",
                "camera_id": "cam1",
                "bearing_deg": 130.0,
                "focus_score": 0.70,
                "activity_score": 0.70,
                "speaking_probability": 0.70,
                "speaking": True,
                "doa_peak_deg": 130.0,
                "score_components": {"doa_peak_score": 0.8},
            }
        ]
        msg = machine.update(cand, vad_state={"t_ns": msg_t_ns_placeholder(), "speech": True})
        self.assertEqual(msg["state"], "LOCKED")
        self.assertEqual(msg["mode"], "AUDIO_ONLY")

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

    def test_uses_steering_bearing_for_target_when_present(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40, "min_switch_interval_ms": 0}}}
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam0-1",
                "bearing_deg": 10.0,
                "steering_bearing_deg": 18.5,
                "combined_score": 0.9,
                "activity_score": 1.0,
                "speaking_probability": 1.0,
                "speaking": True,
                "doa_peak_deg": 20.0,
                "score_components": {"visual_speaking_prob": 1.0, "doa_peak_score": 1.0},
            }
        ]
        msg = machine.update(cand, vad_state=None)
        self.assertEqual(msg["state"], "LOCKED")
        self.assertAlmostEqual(msg["target_bearing_deg"], 18.5, places=6)
        self.assertEqual(msg["selection_mode"], "AV_LOCK")

    def test_locked_state_consumes_updated_steering_bearing(self) -> None:
        config = {"fusion": {"thresholds": {"acquire_threshold": 0.40, "min_switch_interval_ms": 0, "bearing_smoothing_alpha": 1.0}}}
        machine = LockStateMachine(config)
        acquired = [
            {
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 350.0,
                "steering_bearing_deg": 350.0,
                "combined_score": 0.9,
                "activity_score": 1.0,
                "speaking_probability": 1.0,
                "speaking": True,
                "doa_peak_deg": 350.0,
                "score_components": {"visual_speaking_prob": 1.0, "doa_peak_score": 1.0},
            }
        ]
        locked = machine.update(acquired, vad_state=None)
        self.assertEqual(locked["state"], "LOCKED")
        self.assertAlmostEqual(locked["target_bearing_deg"], 350.0, places=6)

        updated = [
            {
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 10.0,
                "steering_bearing_deg": 10.0,
                "combined_score": 0.9,
                "activity_score": 1.0,
                "speaking_probability": 1.0,
                "speaking": True,
                "doa_peak_deg": 10.0,
                "score_components": {"visual_speaking_prob": 1.0, "doa_peak_score": 1.0},
            }
        ]
        maintained = machine.update(updated, vad_state=None)
        self.assertEqual(maintained["state"], "LOCKED")
        self.assertEqual(maintained["reason"], "maintain")
        self.assertAlmostEqual(maintained["target_bearing_deg"], 10.0, places=6)

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

    def test_hold_then_silence_drop(self) -> None:
        config = {
            "fusion": {
                "thresholds": {
                    "acquire_threshold": 0.40,
                    "hold_ms": 200,
                    "min_switch_interval_ms": 0,
                },
                "require_speaking": True,
                "require_vad": False,
            }
        }
        machine = LockStateMachine(config)
        cand = [
            {
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 15.0,
                "focus_score": 0.82,
                "activity_score": 0.81,
                "speaking_probability": 0.81,
                "speaking": True,
                "score_components": {"visual_speaking_prob": 0.81},
            }
        ]
        locked = machine.update(cand, vad_state=None)
        self.assertEqual(locked["state"], "LOCKED")

        machine._last_speaking_ns = now_ns()
        hold = machine.update([], vad_state=None)
        self.assertEqual(hold["state"], "HOLD")
        self.assertEqual(hold["reason"], "silence_hold")

        machine._last_speaking_ns = now_ns() - 400_000_000
        dropped = machine.update([], vad_state=None)
        self.assertEqual(dropped["state"], "NO_LOCK")
        self.assertEqual(dropped["reason"], "silence_drop")

    def test_handoff_commit_and_switch_throttle_reason(self) -> None:
        config = {
            "fusion": {
                "thresholds": {
                    "acquire_threshold": 0.45,
                    "handoff_min_ms": 100,
                    "hold_ms": 500,
                    "min_switch_interval_ms": 500,
                    "bearing_smoothing_alpha": 1.0,
                },
                "require_speaking": False,
                "require_vad": False,
            }
        }
        machine = LockStateMachine(config)
        incumbent = [
            {
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 10.0,
                "focus_score": 0.9,
                "activity_score": 0.8,
                "speaking_probability": 0.8,
                "speaking": True,
                "score_components": {"visual_speaking_prob": 0.8},
            }
        ]
        challenger = [
            {
                "track_id": "cam1-4",
                "camera_id": "cam1",
                "bearing_deg": 135.0,
                "focus_score": 0.85,
                "activity_score": 0.7,
                "speaking_probability": 0.7,
                "speaking": True,
                "score_components": {"visual_speaking_prob": 0.7},
            }
        ]
        locked = machine.update(incumbent, vad_state=None)
        self.assertEqual(locked["state"], "LOCKED")

        start = machine.update(challenger, vad_state=None)
        self.assertEqual(start["state"], "HANDOFF")
        self.assertEqual(start["reason"], "handoff_start")

        machine._handoff_start_ns = now_ns() - 200_000_000
        machine._last_switch_ns = now_ns()
        throttled = machine.update(challenger, vad_state=None)
        self.assertEqual(throttled["state"], "LOCKED")
        self.assertEqual(throttled["reason"], "handoff_switch_throttled")

        machine._last_switch_ns = now_ns() - 600_000_000
        machine._handoff_id = "cam1-4"
        machine._handoff_start_ns = now_ns() - 200_000_000
        committed = machine.update(challenger, vad_state=None)
        self.assertEqual(committed["state"], "LOCKED")
        self.assertEqual(committed["reason"], "handoff_commit")
        self.assertEqual(committed["target_id"], "cam1-4")

    def test_handoff_requires_margin_over_visible_incumbent(self) -> None:
        config = {
            "fusion": {
                "thresholds": {
                    "acquire_threshold": 0.40,
                    "handoff_margin_min": 0.10,
                    "min_switch_interval_ms": 0,
                    "bearing_smoothing_alpha": 1.0,
                },
                "require_speaking": False,
                "require_vad": False,
            }
        }
        machine = LockStateMachine(config)
        incumbent = {
            "track_id": "cam0-1",
            "camera_id": "cam0",
            "bearing_deg": 10.0,
            "focus_score": 0.50,
            "activity_score": 0.4,
            "speaking_probability": 0.4,
            "speaking": True,
            "score_components": {"visual_speaking_prob": 0.4},
        }
        challenger = {
            "track_id": "cam1-4",
            "camera_id": "cam1",
            "bearing_deg": 135.0,
            "focus_score": 0.56,
            "activity_score": 0.6,
            "speaking_probability": 0.6,
            "speaking": True,
            "score_components": {"visual_speaking_prob": 0.6},
        }

        locked = machine.update([incumbent], vad_state=None)
        self.assertEqual(locked["state"], "LOCKED")

        held = machine.update([incumbent, challenger], vad_state=None)
        self.assertEqual(held["state"], "LOCKED")
        self.assertEqual(held["reason"], "handoff_margin_hold")
        self.assertEqual(held["target_id"], "cam0-1")
        self.assertAlmostEqual(held["focus_score"], 0.50, places=6)
        self.assertAlmostEqual(held["runner_up_focus_score"], 0.56, places=6)


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

    def test_candidates_keep_raw_mouth_motion_separate_from_visual_quality_floor(self) -> None:
        tracks = [
            {
                "seq": 1,
                "track_id": "cam1-9",
                "camera_id": "cam1",
                "bearing_deg": 116.0,
                "mouth_activity": 0.18,
                "visual_speaking_prob": 0.27,
                "confidence": 0.8,
                "speaking": False,
                "bbox": {"x": 0, "y": 0, "w": 100, "h": 100},
            }
        ]
        cands = _build_candidates(
            tracks,
            doa_heatmap=None,
            vad_state={"speech": True, "confidence": 1.0, "t_ns": now_ns()},
            max_assoc_deg=20.0,
            weights={},
            min_area=900,
            area_soft_max=3600,
        )
        self.assertEqual(len(cands), 1)
        components = cands[0]["score_components"]
        self.assertAlmostEqual(float(components["explicit_mouth_activity"]), 0.18, places=6)
        self.assertAlmostEqual(float(components["mouth_activity"]), 0.18, places=6)
        self.assertAlmostEqual(float(components["visual_speaking_prob"]), 0.27, places=6)

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

    def test_audio_only_candidate_infers_camera_sector_from_bearing(self) -> None:
        doa_heatmap = {"seq": 1, "confidence": 0.9, "peaks": [{"angle_deg": 118.0, "score": 0.8}]}
        cand = _build_audio_only_candidate(
            doa_heatmap,
            vad_state=None,
            mic_health=None,
            min_doa_confidence=0.45,
            min_peak_score=0.30,
            score_mode="confidence",
            require_vad=False,
            weights={},
            camera_lookup=[
                {"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 160.0},
                {"id": "cam1", "yaw_offset_deg": 120.0, "hfov_deg": 160.0},
                {"id": "cam2", "yaw_offset_deg": 240.0, "hfov_deg": 160.0},
            ],
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand["camera_id"], "cam1")

    def test_av_association_audio_fallback_uses_configured_camera_lookup(self) -> None:
        bus = Bus()
        stop_event = threading.Event()
        q_candidates = bus.subscribe("fusion.candidates")
        thread = start_av_association(
            bus,
            {
                "video": {
                    "cameras": [
                        {"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 160.0},
                        {"id": "cam1", "yaw_offset_deg": 120.0, "hfov_deg": 160.0},
                        {"id": "cam2", "yaw_offset_deg": 240.0, "hfov_deg": 160.0},
                    ]
                },
                "fusion": {
                    "audio_fallback": {
                        "enabled": True,
                        "min_doa_confidence": 0.35,
                        "min_peak_score": 0.22,
                        "score_mode": "max",
                    },
                    "audio_rescue_min": 0.0,
                },
            },
            _NoopLogger(),
            stop_event,
        )
        try:
            bus.publish("audio.doa_heatmap", {"seq": 1, "confidence": 0.7, "peaks": [{"angle_deg": 118.0, "score": 0.9}]})
            msg = q_candidates.get(timeout=1.0)
        except queue.Empty as exc:
            self.fail(f"expected fallback candidate: {exc}")
        finally:
            stop_event.set()
            thread.join(timeout=1.0)

        self.assertEqual(msg["evidence"]["reason"], "audio_rescue")
        self.assertEqual(len(msg["candidates"]), 1)
        self.assertEqual(msg["candidates"][0]["camera_id"], "cam1")

    def test_av_association_suppresses_audio_fallback_after_recent_faces_clear(self) -> None:
        bus = Bus()
        stop_event = threading.Event()
        q_candidates = bus.subscribe("fusion.candidates")
        thread = start_av_association(
            bus,
            {
                "video": {
                    "cameras": [
                        {"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 160.0},
                        {"id": "cam1", "yaw_offset_deg": 120.0, "hfov_deg": 160.0},
                    ]
                },
                "fusion": {
                    "visual_freshness_ms": 1200,
                    "audio_fallback": {
                        "enabled": True,
                        "min_doa_confidence": 0.35,
                        "min_peak_score": 0.22,
                        "score_mode": "max",
                    },
                    "audio_rescue_min": 0.0,
                },
            },
            _NoopLogger(),
            stop_event,
        )
        try:
            bus.publish(
                "vision.face_tracks",
                [
                    {
                        "seq": 1,
                        "track_id": "cam0-1",
                        "camera_id": "cam0",
                        "bearing_deg": 0.0,
                        "mouth_activity": 0.0,
                        "visual_speaking_prob": 0.0,
                        "confidence": 1.0,
                        "speaking": False,
                    }
                ],
            )
            first = q_candidates.get(timeout=1.0)
            self.assertEqual(first["evidence"]["reason"], "faces_only")

            bus.publish("vision.face_tracks", [])
            time.sleep(0.02)
            bus.publish("audio.doa_heatmap", {"seq": 2, "confidence": 0.8, "peaks": [{"angle_deg": 118.0, "score": 0.9}]})

            msg = None
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                candidate_msg = q_candidates.get(timeout=max(0.01, deadline - time.monotonic()))
                if candidate_msg["evidence"]["reason"] == "recent_faces_unassociated":
                    msg = candidate_msg
                    break
            if msg is None:
                self.fail("expected recent_faces_unassociated candidate envelope")
        except queue.Empty as exc:
            self.fail(f"expected candidate envelope: {exc}")
        finally:
            stop_event.set()
            thread.join(timeout=1.0)

        self.assertEqual(msg["candidates"], [])
        self.assertTrue(bool(msg["evidence"]["faces_present"]))
        self.assertTrue(bool(msg["evidence"]["faces_fresh"]))

    def test_visual_first_scoring_prefers_strong_visual_face_over_audio_aligned_weak_face(self) -> None:
        weights = {
            "mouth": 0.9,
            "face": 0.4,
            "doa": 1.1,
            "doa_confidence": 0.8,
            "audio": 1.0,
            "angle": 0.9,
            "continuity": 0.2,
        }
        tracks = [
            {
                "seq": 1,
                "track_id": "visual-strong",
                "camera_id": "cam0",
                "bearing_deg": 0.0,
                "mouth_activity": 0.95,
                "visual_speaking_prob": 0.95,
                "confidence": 0.9,
                "speaking": True,
                "bbox": {"x": 0, "y": 0, "w": 100, "h": 100},
            },
            {
                "seq": 1,
                "track_id": "audio-aligned-weak",
                "camera_id": "cam1",
                "bearing_deg": 90.0,
                "mouth_activity": 0.12,
                "visual_speaking_prob": 0.12,
                "confidence": 0.8,
                "speaking": False,
                "bbox": {"x": 0, "y": 0, "w": 100, "h": 100},
            },
        ]
        doa_heatmap = {
            "confidence": 0.95,
            "peaks": [{"angle_deg": 90.0, "score": 0.96}],
        }
        cands = _build_candidates(
            tracks,
            doa_heatmap=doa_heatmap,
            max_assoc_deg=20.0,
            weights=weights,
            min_area=900,
            area_soft_max=3600,
            visual_override_min=0.6,
            disagreement_penalty=0.7,
        )
        by_id = {c["track_id"]: c for c in cands}
        self.assertGreater(
            float(by_id["visual-strong"]["focus_score"]),
            float(by_id["audio-aligned-weak"]["focus_score"]),
        )
        self.assertGreaterEqual(float(by_id["visual-strong"]["score_groups"]["visual_score"]), 0.6)
        self.assertGreater(float(by_id["audio-aligned-weak"]["score_groups"]["disagreement_penalty"]), 0.0)

    def test_candidate_steering_bearing_blends_toward_doa_inside_camera_sector(self) -> None:
        config = {
            "video": {
                "cameras": [
                    {"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 90.0},
                ]
            }
        }
        tracks = [
            {
                "seq": 1,
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "visual_speaking_prob": 1.0,
                "confidence": 1.0,
                "speaking": True,
            }
        ]
        doa_heatmap = {"confidence": 0.95, "peaks": [{"angle_deg": 20.0, "score": 0.9}]}
        cands = _build_candidates(
            tracks,
            doa_heatmap=doa_heatmap,
            max_assoc_deg=20.0,
            weights={},
            min_area=900,
            area_soft_max=3600,
            camera_lookup=config["video"]["cameras"],
        )
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(float(cand["bearing_deg"]), 10.0)
        self.assertIn("steering_bearing_deg", cand)
        self.assertGreater(float(cand["steering_bearing_deg"]), 10.0)
        self.assertLess(float(cand["steering_bearing_deg"]), 20.0)

    def test_candidate_steering_bearing_uses_track_id_camera_when_camera_id_missing(self) -> None:
        cameras = [{"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 90.0}]
        tracks = [
            {
                "seq": 1,
                "track_id": "cam0-1",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "visual_speaking_prob": 1.0,
                "confidence": 1.0,
                "speaking": True,
            }
        ]
        cands = _build_candidates(
            tracks,
            doa_heatmap={"confidence": 0.95, "peaks": [{"angle_deg": 20.0, "score": 0.9}]},
            max_assoc_deg=20.0,
            weights={},
            min_area=900,
            area_soft_max=3600,
            camera_lookup=cameras,
        )
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["track_id"], "cam0-1")
        self.assertGreater(float(cand["steering_bearing_deg"]), 10.0)
        self.assertLess(float(cand["steering_bearing_deg"]), 20.0)

    def test_candidate_steering_bearing_stays_on_face_when_doa_outside_or_absent(self) -> None:
        cameras = [{"id": "cam0", "yaw_offset_deg": 0.0, "hfov_deg": 30.0}]
        tracks = [
            {
                "seq": 1,
                "track_id": "cam0-1",
                "camera_id": "cam0",
                "bearing_deg": 10.0,
                "mouth_activity": 1.0,
                "visual_speaking_prob": 1.0,
                "confidence": 1.0,
                "speaking": True,
            }
        ]
        outside = _build_candidates(
            tracks,
            doa_heatmap={"confidence": 0.95, "peaks": [{"angle_deg": 20.0, "score": 0.9}]},
            max_assoc_deg=20.0,
            weights={},
            min_area=900,
            area_soft_max=3600,
            camera_lookup=cameras,
        )[0]
        absent = _build_candidates(
            tracks,
            doa_heatmap=None,
            max_assoc_deg=20.0,
            weights={},
            min_area=900,
            area_soft_max=3600,
            camera_lookup=cameras,
        )[0]
        self.assertEqual(float(outside["steering_bearing_deg"]), 10.0)
        self.assertEqual(float(absent["steering_bearing_deg"]), 10.0)


def msg_t_ns_placeholder() -> int:
    # LockStateMachine only needs a numeric `t_ns` for freshness checks. Tests don't
    # depend on actual monotonic time.
    return 1


if __name__ == "__main__":
    unittest.main()
