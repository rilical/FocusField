"""
CONTRACT: inline (source: src/focusfield/fusion/confidence.md)
ROLE: Combine candidate scores into confidence.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - fusion.score_weights: component weights

PERF / TIMING:
  - per candidate

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/fusion/confidence.md):
# Confidence model

- Combine mouth activity, face confidence, DOA peak score.
- Penalize large angular distance.
- Normalize to 0..1 for lock logic.
"""

from __future__ import annotations

from typing import Dict

from focusfield.fusion.speaker_posterior import estimate_speaker_posterior


def combine_scores(
    mouth_activity: float,
    face_confidence: float,
    doa_peak_score: float,
    doa_confidence: float,
    angle_error_deg: float,
    audio_speech_prob: float,
    track_continuity: float,
    mic_health_score: float,
    weights: Dict[str, float],
) -> float:
    """Combine evidence into a calibrated 0..1 active-speaker posterior."""
    posterior_weights = {
        "bias": float(weights.get("bias", -0.35)),
        "visual": float(weights.get("mouth", weights.get("visual", 1.35))),
        "face": float(weights.get("face", 0.35)),
        "doa_peak": float(weights.get("doa", weights.get("doa_peak", 1.15))),
        "doa_confidence": float(weights.get("doa_confidence", 0.75)),
        "audio": float(weights.get("audio", 1.05)),
        "angle_match": float(weights.get("angle", weights.get("angle_match", 0.95))),
        "continuity": float(weights.get("continuity", 0.55)),
        "mic_health": float(weights.get("mic_health", 0.80)),
        "agreement_bonus": float(weights.get("agreement_bonus", 0.40)),
    }
    return estimate_speaker_posterior(
        visual_speaking_prob=mouth_activity,
        face_confidence=face_confidence,
        doa_peak_score=doa_peak_score,
        doa_confidence=doa_confidence,
        angle_error_deg=angle_error_deg,
        audio_speech_prob=audio_speech_prob,
        track_continuity=track_continuity,
        mic_health_score=mic_health_score,
        weights=posterior_weights,
    )
