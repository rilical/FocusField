"""focusfield.audio.sync.drift_check

CONTRACT: inline (source: src/focusfield/audio/sync/drift_check.md)
ROLE: Detect multi-channel drift / misalignment.

For a true USB mic array, channels should be sample-synchronous.
This check uses a GCC-PHAT style correlation peak between each channel
and channel 0. If the peak offset exceeds a threshold, it emits a log.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame

OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.sync.enabled: enable drift check
  - audio.sync.drift_check.max_offset_samples: threshold
  - audio.sync.drift_check.check_every_s: rate

LOG EVENTS:
  - module=audio.sync.drift_check, event=drift_exceeded
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Optional

import numpy as np


def start_drift_check(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    sync_cfg = config.get("audio", {}).get("sync", {})
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}
    if not bool(sync_cfg.get("enabled", False)):
        return None
    drift_cfg = sync_cfg.get("drift_check", {})
    if not isinstance(drift_cfg, dict):
        drift_cfg = {}
    max_offset = int(drift_cfg.get("max_offset_samples", 6))
    check_every_s = float(drift_cfg.get("check_every_s", 2.0))
    check_every_s = max(0.2, check_every_s)

    q = bus.subscribe("audio.frames")

    def _run() -> None:
        last_check = 0.0
        while not stop_event.is_set():
            try:
                frame_msg = q.get(timeout=0.1)
            except queue.Empty:
                continue
            now_s = time.time()
            if now_s - last_check < check_every_s:
                continue
            last_check = now_s
            data = frame_msg.get("data")
            if data is None:
                continue
            x = np.asarray(data)
            if x.ndim != 2 or x.shape[1] < 2:
                continue
            ref = x[:, 0].astype(np.float32)
            if not np.any(np.isfinite(ref)):
                continue
            offsets = []
            for ch in range(1, x.shape[1]):
                sig = x[:, ch].astype(np.float32)
                off = _estimate_offset_samples(ref, sig)
                offsets.append(int(off))
            worst = int(max(abs(o) for o in offsets)) if offsets else 0
            if worst > max_offset:
                logger.emit(
                    "warning",
                    "audio.sync.drift_check",
                    "drift_exceeded",
                    {"max_offset_samples": worst, "offsets": offsets, "threshold": max_offset},
                )

    thread = threading.Thread(target=_run, name="drift-check", daemon=True)
    thread.start()
    return thread


def _estimate_offset_samples(x: np.ndarray, y: np.ndarray) -> int:
    """Return lag in samples (positive means y lags x)."""
    if x.size == 0 or y.size == 0:
        return 0
    n = int(2 ** np.ceil(np.log2(max(x.size, y.size) * 2)))
    X = np.fft.rfft(x, n=n)
    Y = np.fft.rfft(y, n=n)
    R = X * np.conj(Y)
    denom = np.abs(R)
    R = R / np.maximum(denom, 1e-12)
    cc = np.fft.irfft(R, n=n)
    max_shift = int(min(x.size, y.size) // 2)
    cc = np.concatenate((cc[-max_shift:], cc[: max_shift + 1]))
    shift = int(np.argmax(np.abs(cc)) - max_shift)
    return shift

