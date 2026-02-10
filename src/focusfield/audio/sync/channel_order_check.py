"""focusfield.audio.sync.channel_order_check

CONTRACT: inline (source: src/focusfield/audio/sync/channel_order_check.md)
ROLE: Helper for channel mapping verification.

This module is intentionally thin. The primary UX is the standalone script
`scripts/calibrate_uma8.py`, which guides the user through a tap test.

Here we provide shared helpers so other scripts can reuse the logic.
"""

from __future__ import annotations

from typing import List

import numpy as np


def detect_tap_channel(frame: np.ndarray) -> int:
    """Return channel index with highest RMS energy."""
    x = np.asarray(frame)
    if x.ndim != 2:
        raise ValueError("Expected (samples, channels) array")
    rms = np.sqrt(np.mean(x.astype(np.float32) ** 2, axis=0))
    return int(np.argmax(rms))


def build_channel_order(observed_ring: List[int], total_channels: int) -> List[int]:
    """Build full channel_order by appending remaining channels."""
    remaining = [ch for ch in range(int(total_channels)) if ch not in observed_ring]
    return list(observed_ring) + remaining

