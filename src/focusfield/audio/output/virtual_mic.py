"""
CONTRACT: inline (source: src/focusfield/audio/output/virtual_mic.md)
ROLE: Virtual mic routing placeholder.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.sink: virtual_mic
  - output.virtual_mic.device_index: explicit output device index (optional)
  - output.virtual_mic.device_selector.match_substring: substring match on device name (optional)
  - output.virtual_mic.channels: output channel count (default: 2)
  - output.virtual_mic.sample_rate_hz: override output sample rate (default: audio.sample_rate_hz)
  - output.virtual_mic.buffer_blocks: jitter buffer depth (default: 16)

PERF / TIMING:
  - n/a

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=audio.output.virtual_mic, event=started
  - module=audio.output.virtual_mic, event=underrun
  - module=audio.output.virtual_mic, event=device_error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/output/virtual_mic.md):
# Virtual mic routing (no code)

- OS-specific routing plan for virtual mic output.
- Document device names and expected sample rate.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None


@dataclass(frozen=True)
class AudioOutputDeviceInfo:
    index: int
    name: str
    hostapi: Optional[str]
    max_output_channels: int
    default_samplerate_hz: Optional[float]


def list_output_devices() -> List[AudioOutputDeviceInfo]:
    """Return a normalized list of output-capable audio devices."""
    if sd is None:
        return []
    hostapis = []
    try:
        hostapis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        hostapis = []
    hostapi_names = {idx: api.get("name") for idx, api in enumerate(hostapis) if isinstance(api, dict)}

    devices: List[AudioOutputDeviceInfo] = []
    for idx, raw in enumerate(sd.query_devices()):
        if not isinstance(raw, dict):
            continue
        max_out = int(raw.get("max_output_channels") or 0)
        if max_out <= 0:
            continue
        hostapi_idx = raw.get("hostapi")
        hostapi_name = hostapi_names.get(hostapi_idx) if isinstance(hostapi_idx, int) else None
        devices.append(
            AudioOutputDeviceInfo(
                index=idx,
                name=str(raw.get("name") or ""),
                hostapi=hostapi_name,
                max_output_channels=max_out,
                default_samplerate_hz=_as_float_or_none(raw.get("default_samplerate")),
            )
        )
    return devices


def resolve_output_device_index(config: Dict[str, Any], logger: Any = None) -> Optional[int]:
    """Resolve an output device index from config.

    Priority:
      1) output.virtual_mic.device_index
      2) output.virtual_mic.device_selector.match_substring
      3) default output device (None)
    """
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    vm_cfg = output_cfg.get("virtual_mic", {})
    if not isinstance(vm_cfg, dict):
        vm_cfg = {}

    if "device_index" in vm_cfg and vm_cfg.get("device_index") is not None:
        return int(vm_cfg["device_index"])

    selector = vm_cfg.get("device_selector", {})
    if not isinstance(selector, dict):
        selector = {}
    match_substring = selector.get("match_substring")

    if match_substring is not None:
        target = str(match_substring).strip().lower()
        if target:
            devices = list_output_devices()
            matching = [d for d in devices if target in d.name.lower()]
            if matching:
                chosen = max(matching, key=lambda d: d.max_output_channels)
                _log(logger, "info", "audio.output.virtual_mic", "device_selected", {"device": asdict(chosen)})
                return chosen.index
    return None


def start_virtual_mic_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    if sd is None:
        logger.emit("error", "audio.output.virtual_mic", "backend_missing", {"backend": "sounddevice"})
        return None

    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    vm_cfg = output_cfg.get("virtual_mic", {})
    if not isinstance(vm_cfg, dict):
        vm_cfg = {}

    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    sample_rate = int(vm_cfg.get("sample_rate_hz", audio_cfg.get("sample_rate_hz", 48000)))
    block_size = int(audio_cfg.get("block_size", 1024))
    channels = int(vm_cfg.get("channels", 2))
    channels = max(1, channels)
    device_index = resolve_output_device_index(config, logger)
    selector = vm_cfg.get("device_selector", {})
    if not isinstance(selector, dict):
        selector = {}
    match_substring = selector.get("match_substring")
    if match_substring is not None and str(match_substring).strip() and device_index is None and vm_cfg.get("device_index") is None:
        devices = list_output_devices()
        logger.emit(
            "error",
            "audio.output.virtual_mic",
            "device_not_found",
            {
                "match_substring": str(match_substring),
                "available_count": int(len(devices)),
                "available_names": [d.name for d in devices[:10]],
            },
        )
        stop_event.set()
        return None

    q_bus = bus.subscribe("audio.enhanced.final")
    # Small jitter buffer so callback can run without blocking.
    q_audio: queue.Queue[np.ndarray] = queue.Queue(maxsize=int(vm_cfg.get("buffer_blocks", 16)))

    drop_throttle: Dict[str, float] = {}

    def _throttled(level: str, event: str, payload: Dict[str, Any], throttle_s: float = 0.25) -> None:
        now_s = time.time()
        last = drop_throttle.get(event, 0.0)
        if now_s - last < throttle_s:
            return
        drop_throttle[event] = now_s
        try:
            logger.emit(level, "audio.output.virtual_mic", event, payload)
        except Exception:  # noqa: BLE001
            return

    def _put_drop_oldest(q: queue.Queue[np.ndarray], item: np.ndarray) -> None:
        try:
            q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def _worker() -> None:
        while not stop_event.is_set():
            try:
                msg = q_bus.get(timeout=0.1)
            except queue.Empty:
                continue
            data = msg.get("data")
            if data is None:
                continue
            sr_msg = msg.get("sample_rate_hz")
            if sr_msg is not None and int(sr_msg) != sample_rate:
                _throttled(
                    "warning",
                    "sample_rate_mismatch",
                    {"expected_hz": sample_rate, "got_hz": int(sr_msg)},
                    throttle_s=1.0,
                )
            x = np.asarray(data, dtype=np.float32).reshape(-1)
            if x.size == 0:
                continue
            _put_drop_oldest(q_audio, x)

    worker = threading.Thread(target=_worker, name="virtual-mic-worker", daemon=True)
    worker.start()

    def _callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
        if status:
            _throttled("warning", "underrun", {"status": str(status)}, throttle_s=0.5)
        try:
            x = q_audio.get_nowait()
        except queue.Empty:
            outdata[:] = 0.0
            _throttled("warning", "underrun", {"status": "buffer_empty"}, throttle_s=0.5)
            return
        if x.shape[0] < frames:
            mono = np.zeros((frames,), dtype=np.float32)
            mono[: x.shape[0]] = x
            _throttled("warning", "block_size_mismatch", {"expected_frames": int(frames), "got": int(x.shape[0])}, throttle_s=1.0)
        elif x.shape[0] > frames:
            mono = x[:frames]
            _throttled("warning", "block_size_mismatch", {"expected_frames": int(frames), "got": int(x.shape[0])}, throttle_s=1.0)
        else:
            mono = x
        if channels == 1:
            outdata[:, 0] = mono
        else:
            outdata[:] = mono[:, None]

    def _run() -> None:
        try:
            devices = list_output_devices()
            chosen = next((d for d in devices if device_index is not None and d.index == device_index), None)
            logger.emit(
                "info",
                "audio.output.virtual_mic",
                "started",
                {
                    "device_index": int(device_index) if device_index is not None else None,
                    "device_name": str(chosen.name) if chosen is not None else None,
                    "device_hostapi": str(chosen.hostapi) if chosen is not None else None,
                    "channels": int(channels),
                    "sample_rate_hz": int(sample_rate),
                    "block_size": int(block_size),
                },
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            stream = sd.OutputStream(
                samplerate=int(sample_rate),
                blocksize=int(block_size),
                channels=int(channels),
                dtype="float32",
                device=device_index,
                callback=_callback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.emit(
                "error",
                "audio.output.virtual_mic",
                "device_error",
                {"error": str(exc), "device_index": int(device_index) if device_index is not None else None},
            )
            stop_event.set()
            return

        try:
            with stream:
                while not stop_event.is_set():
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            logger.emit("error", "audio.output.virtual_mic", "device_error", {"error": str(exc)})
            stop_event.set()

    thread = threading.Thread(target=_run, name="virtual-mic", daemon=True)
    thread.start()
    return thread


def _as_float_or_none(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _log(logger: Any, level: str, module: str, event: str, payload: Dict[str, Any]) -> None:
    if logger is None:
        return
    try:
        logger.emit(level, module, event, payload)
    except Exception:  # noqa: BLE001
        return
