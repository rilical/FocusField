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


def combine_scores(
    mouth_activity: float,
    face_confidence: float,
    doa_peak_score: float,
    angle_error_deg: float,
    weights: Dict[str, float],
    doa_alignment: float = 0.0,
    facing_score: float = 1.0,
    interrupt_bonus: float = 0.0,
) -> float:
    """Combine component scores into a 0..1 confidence."""
    w_mouth = float(weights.get("mouth", 0.7))
    w_face = float(weights.get("face", 0.3))
    w_doa = float(weights.get("doa", 0.0))
    w_angle = float(weights.get("angle", 0.0))
    angle_penalty = max(0.0, min(1.0, angle_error_deg / 180.0))
    doa_joint = 0.5 * max(0.0, min(1.0, doa_peak_score)) + 0.5 * max(0.0, min(1.0, doa_alignment))
    facing = max(0.0, min(1.0, facing_score))
    score = (
        w_mouth * mouth_activity
        + w_face * (face_confidence * facing)
        + w_doa * doa_joint
        - w_angle * angle_penalty
        + max(0.0, min(1.0, interrupt_bonus))
    )
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score
