"""
ROLE: Reliability-aware posterior scoring for active speaker selection.
"""

from __future__ import annotations

import math
from typing import Dict


def estimate_speaker_posterior(
    visual_speaking_prob: float,
    face_confidence: float,
    doa_peak_score: float,
    doa_confidence: float,
    angle_error_deg: float,
    audio_speech_prob: float,
    track_continuity: float,
    mic_health_score: float,
    weights: Dict[str, float],
) -> float:
    """Estimate P(track is active speaker) from calibrated evidence."""
    visual = _clamp01(visual_speaking_prob)
    face = _clamp01(face_confidence)
    doa_peak = _clamp01(doa_peak_score)
    doa_conf = _clamp01(doa_confidence)
    audio = _clamp01(audio_speech_prob)
    continuity = _clamp01(track_continuity)
    mic_health = _clamp01(mic_health_score)
    angle_match = 1.0 - _clamp01(angle_error_deg / 90.0)

    score = float(weights.get("bias", -0.35))
    score += float(weights.get("visual", 1.35)) * _centered(visual)
    score += float(weights.get("face", 0.35)) * _centered(face)
    score += float(weights.get("doa_peak", 1.15)) * _centered(doa_peak)
    score += float(weights.get("doa_confidence", 0.75)) * _centered(doa_conf)
    score += float(weights.get("audio", 1.05)) * _centered(audio)
    score += float(weights.get("angle_match", 0.95)) * _centered(angle_match)
    score += float(weights.get("continuity", 0.55)) * _centered(continuity)
    score += float(weights.get("mic_health", 0.80)) * _centered(mic_health)
    score += float(weights.get("agreement_bonus", 0.40)) * (visual * doa_peak * audio)
    return _sigmoid(score)


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _centered(value: float) -> float:
    return (2.0 * float(value)) - 1.0


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)
