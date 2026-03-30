"""
CONTRACT: inline (source: src/focusfield/audio/enhance/agc_post.md)
ROLE: Optional post-AGC / limiter utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class AdaptiveGainLimiter:
    """Lightweight post-AGC with a soft limiter for meeting output."""

    enabled: bool = False
    target_rms: float = 0.1
    max_gain: float = 4.0
    min_gain: float = 0.4
    attack_alpha: float = 0.30
    release_alpha: float = 0.94
    limiter_threshold: float = 0.92
    _gain: float = 1.0

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AdaptiveGainLimiter":
        audio_cfg = config.get("audio", {})
        if not isinstance(audio_cfg, dict):
            audio_cfg = {}
        agc_cfg = audio_cfg.get("agc_post", {})
        if not isinstance(agc_cfg, dict):
            agc_cfg = {}
        return cls(
            enabled=bool(agc_cfg.get("enabled", False)),
            target_rms=float(agc_cfg.get("target_rms", 0.1) or 0.1),
            max_gain=float(agc_cfg.get("max_gain", 4.0) or 4.0),
            min_gain=float(agc_cfg.get("min_gain", 0.4) or 0.4),
            attack_alpha=float(agc_cfg.get("attack_alpha", 0.30) or 0.30),
            release_alpha=float(agc_cfg.get("release_alpha", 0.94) or 0.94),
            limiter_threshold=float(agc_cfg.get("limiter_threshold", 0.92) or 0.92),
        )

    def process(self, frame: np.ndarray, logger: Any = None, module_name: str = "audio.enhance.agc_post") -> Tuple[np.ndarray, Dict[str, float]]:
        x = np.asarray(frame, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x, {"gain": float(self._gain), "rms": 0.0, "peak": 0.0, "clipped": 0.0}
        rms = float(np.sqrt(np.mean(x**2)))
        peak = float(np.max(np.abs(x)))
        if not self.enabled:
            return x, {"gain": 1.0, "rms": rms, "peak": peak, "clipped": 0.0}

        desired_gain = self.target_rms / max(rms, 1e-5)
        desired_gain = float(np.clip(desired_gain, self.min_gain, self.max_gain))
        alpha = self.attack_alpha if desired_gain < self._gain else self.release_alpha
        alpha = float(np.clip(alpha, 0.0, 0.999))
        self._gain = alpha * float(self._gain) + (1.0 - alpha) * desired_gain

        y = x * float(self._gain)
        threshold = float(np.clip(self.limiter_threshold, 0.1, 0.999))
        clipped_fraction = 0.0
        peak_after = float(np.max(np.abs(y)))
        if peak_after > threshold:
            clipped_fraction = float(np.mean(np.abs(y) >= threshold))
            # Soft saturate before hard clipping to preserve speech character.
            y = threshold * np.tanh(y / threshold)
            if logger is not None:
                try:
                    logger.emit(
                        "warning",
                        module_name,
                        "clipping",
                        {
                            "rms": rms,
                            "peak_before": peak_after,
                            "threshold": threshold,
                            "clipped_fraction": clipped_fraction,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
        y = np.clip(y, -0.999, 0.999).astype(np.float32, copy=False)
        return y, {
            "gain": float(self._gain),
            "rms": float(np.sqrt(np.mean(y**2))) if y.size else 0.0,
            "peak": float(np.max(np.abs(y))) if y.size else 0.0,
            "clipped": clipped_fraction,
        }
