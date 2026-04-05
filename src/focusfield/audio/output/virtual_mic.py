"""
CONTRACT: inline (source: src/focusfield/audio/output/virtual_mic.md)
ROLE: Host loopback and USB-mic device sinks.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional

import numpy as np

from focusfield.audio.enhance.agc_post import AdaptiveGainLimiter
from focusfield.core.clock import now_ns

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
        devices.append(
            AudioOutputDeviceInfo(
                index=idx,
                name=str(raw.get("name") or ""),
                hostapi=hostapi_names.get(hostapi_idx) if isinstance(hostapi_idx, int) else None,
                max_output_channels=max_out,
                default_samplerate_hz=_as_float_or_none(raw.get("default_samplerate")),
            )
        )
    return devices


def resolve_output_device_index(
    config: Dict[str, Any],
    section_name: str = "host_loopback",
    logger: Any = None,
) -> Optional[int]:
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    section = _sink_cfg(output_cfg, section_name)
    if "device_index" in section and section.get("device_index") is not None:
        return int(section["device_index"])

    selector = section.get("device_selector", {})
    if not isinstance(selector, dict):
        selector = {}
    devices = list_output_devices()
    hostapi = selector.get("hostapi")
    if hostapi is not None:
        hostapi_target = str(hostapi).strip().lower()
        if hostapi_target:
            devices = [device for device in devices if str(device.hostapi or "").strip().lower() == hostapi_target]
    exact_name = selector.get("exact_name")
    if exact_name is not None:
        target = str(exact_name).strip()
        if target:
            matches = [device for device in devices if device.name.strip() == target]
            if matches:
                chosen = max(matches, key=lambda item: item.max_output_channels)
                _log(logger, "info", _module_name(section_name), "device_selected", {"device": asdict(chosen)})
                return chosen.index
    match_substring = selector.get("match_substring")
    if match_substring is not None:
        target = str(match_substring).strip().lower()
        if target:
            matches = [device for device in devices if target in device.name.lower()]
            if matches:
                chosen = max(matches, key=lambda item: item.max_output_channels)
                _log(logger, "info", _module_name(section_name), "device_selected", {"device": asdict(chosen)})
                return chosen.index
    return None


def start_virtual_mic_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    return start_host_loopback_sink(bus, config, logger, stop_event)


def start_host_loopback_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    return _start_device_sink(bus, config, logger, stop_event, section_name="host_loopback")


def start_usb_mic_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    return _start_device_sink(bus, config, logger, stop_event, section_name="usb_mic")


def _start_device_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
    section_name: str,
) -> Optional[threading.Thread]:
    if sd is None:
        logger.emit("error", _module_name(section_name), "backend_missing", {"backend": "sounddevice"})
        return None

    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    sink_cfg = _sink_cfg(output_cfg, section_name)
    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}

    sample_rate = int(sink_cfg.get("sample_rate_hz", audio_cfg.get("sample_rate_hz", 48000)))
    block_size = int(audio_cfg.get("block_size", 1024))
    channels = max(1, int(sink_cfg.get("channels", 1)))
    buffer_blocks = max(4, int(sink_cfg.get("buffer_blocks", 16) or 16))
    target_buffer_blocks = max(2, int(sink_cfg.get("target_buffer_blocks", max(3, buffer_blocks // 3)) or max(3, buffer_blocks // 3)))
    reconnect_delay_ms = max(100, int(sink_cfg.get("reconnect_delay_ms", 750) or 750))
    drift_correction_ppm = float(sink_cfg.get("drift_correction_ppm", 500.0) or 500.0)
    max_buffer_frames = int(block_size * buffer_blocks)
    target_buffer_frames = int(block_size * min(buffer_blocks - 1, target_buffer_blocks))
    module_name = _module_name(section_name)
    q_bus = bus.subscribe("audio.enhanced.final")
    q_shed = bus.subscribe("runtime.shed_state")
    agc = AdaptiveGainLimiter.from_config(config)

    state_lock = threading.Lock()
    buffer: Deque[np.ndarray] = deque()
    buffered_frames = 0
    source_remainder = np.zeros((0,), dtype=np.float32)
    output_remainder = np.zeros((0,), dtype=np.float32)
    resample_ratio = 1.0
    source_sample_rate_hz = sample_rate
    latest_stage_timestamps: Dict[str, Any] = {}
    latest_input_t_ns = 0
    latest_shed_state: Dict[str, Any] = {"active": False, "level": 0, "reason": "normal", "targets": []}
    publish_interval_s = 1.0
    heartbeat_interval_s = 1.0
    last_heartbeat_s = time.time()
    processed_cycles = 0
    underrun_cycles = 0
    last_consume_underrun = False
    stats = {
        "underrun_window": 0,
        "underrun_total": 0,
        "overrun_window": 0,
        "overrun_total": 0,
        "device_error_window": 0,
        "device_error_total": 0,
        "sample_rate_mismatch_window": 0,
        "sample_rate_mismatch_total": 0,
        "block_size_mismatch_window": 0,
        "block_size_mismatch_total": 0,
    }
    stats_last_emit_s = time.time()

    def _publish_stats(device_name: Optional[str]) -> None:
        nonlocal stats_last_emit_s
        now_s = time.time()
        if now_s - stats_last_emit_s < publish_interval_s:
            return
        stats_last_emit_s = now_s
        with state_lock:
            occupancy_frames = int(buffered_frames + output_remainder.shape[0])
            ratio = float(resample_ratio)
            src_sr = int(source_sample_rate_hz)
            stage_timestamps = dict(latest_stage_timestamps)
            input_age_ms = (now_ns() - int(latest_input_t_ns)) / 1_000_000.0 if latest_input_t_ns else None
            payload = {
                "t_ns": now_ns(),
                "sink": section_name,
                "backend": str(sink_cfg.get("backend", "sounddevice") or "sounddevice"),
                "device_name": device_name,
                "sample_rate_hz": int(sample_rate),
                "source_sample_rate_hz": src_sr,
                "occupancy_frames": occupancy_frames,
                "target_buffer_frames": int(target_buffer_frames),
                "buffer_capacity_frames": int(max_buffer_frames),
                "resample_ratio": ratio,
                "input_age_ms": float(input_age_ms) if input_age_ms is not None else None,
                "stage_timestamps": stage_timestamps,
                "shed_state": dict(latest_shed_state),
                **stats,
            }
            for key in list(stats.keys()):
                if key.endswith("_window"):
                    stats[key] = 0
        bus.publish("audio.output.stats", payload)

    def _enqueue(block: np.ndarray) -> None:
        nonlocal buffered_frames
        if block.size == 0:
            return
        with state_lock:
            buffer.append(block.astype(np.float32, copy=False))
            buffered_frames += int(block.shape[0])
            overflow = max(0, buffered_frames - max_buffer_frames)
            if overflow > 0:
                while overflow > 0 and buffer:
                    head = buffer[0]
                    if head.shape[0] <= overflow:
                        overflow -= int(head.shape[0])
                        buffered_frames -= int(head.shape[0])
                        buffer.popleft()
                    else:
                        buffer[0] = head[overflow:]
                        buffered_frames -= overflow
                        overflow = 0
                stats["overrun_window"] += 1
                stats["overrun_total"] += 1

    def _consume(frames: int) -> np.ndarray:
        nonlocal buffered_frames
        nonlocal output_remainder
        nonlocal last_consume_underrun
        with state_lock:
            if output_remainder.shape[0] >= frames:
                out = output_remainder[:frames]
                output_remainder = output_remainder[frames:]
                last_consume_underrun = False
                return out
            chunks: List[np.ndarray] = []
            if output_remainder.size:
                chunks.append(output_remainder)
                output_remainder = np.zeros((0,), dtype=np.float32)
            needed = frames - sum(chunk.shape[0] for chunk in chunks)
            while needed > 0 and buffer:
                head = buffer.popleft()
                buffered_frames -= int(head.shape[0])
                chunks.append(head)
                needed = frames - sum(chunk.shape[0] for chunk in chunks)
            if chunks:
                merged = np.concatenate(chunks, axis=0)
            else:
                merged = np.zeros((0,), dtype=np.float32)
            if merged.shape[0] < frames:
                stats["underrun_window"] += 1
                stats["underrun_total"] += 1
                last_consume_underrun = True
                padded = np.zeros((frames,), dtype=np.float32)
                padded[: merged.shape[0]] = merged
                return padded
            if merged.shape[0] > frames:
                output_remainder = merged[frames:]
                last_consume_underrun = False
                return merged[:frames]
            last_consume_underrun = False
            return merged

    def _adjust_ratio(current_occupancy: int) -> float:
        if target_buffer_frames <= 0:
            return 1.0
        error = float(current_occupancy - target_buffer_frames) / float(max(1, target_buffer_frames))
        correction = np.clip(error * (drift_correction_ppm * 1e-6), -drift_correction_ppm * 1e-6, drift_correction_ppm * 1e-6)
        return float(np.clip(1.0 - correction, 0.995, 1.005))

    def _ingest_loop() -> None:
        nonlocal source_remainder
        nonlocal resample_ratio
        nonlocal source_sample_rate_hz
        nonlocal latest_stage_timestamps
        nonlocal latest_input_t_ns
        nonlocal latest_shed_state
        nonlocal publish_interval_s
        while not stop_event.is_set():
            try:
                msg = q_bus.get(timeout=0.1)
            except Exception:
                continue
            data = msg.get("data")
            if data is None:
                continue
            x = np.asarray(data, dtype=np.float32).reshape(-1)
            source_sample_rate_hz = int(msg.get("sample_rate_hz", sample_rate) or sample_rate)
            latest_input_t_ns = int(msg.get("t_ns", 0) or 0)
            stage_timestamps = msg.get("stage_timestamps", {})
            if isinstance(stage_timestamps, dict):
                latest_stage_timestamps = dict(stage_timestamps)
            shed_msg = None
            try:
                while True:
                    shed_msg = q_shed.get_nowait()
            except Exception:
                pass
            if isinstance(shed_msg, dict):
                latest_shed_state = dict(shed_msg)
                level = int(latest_shed_state.get("level", 0) or 0)
                publish_interval_s = 2.0 if level >= 2 else 1.0
            if source_sample_rate_hz != sample_rate:
                with state_lock:
                    stats["sample_rate_mismatch_window"] += 1
                    stats["sample_rate_mismatch_total"] += 1
            y, _agc_stats = agc.process(x, logger=logger, module_name=module_name)
            with state_lock:
                occupancy = int(buffered_frames + output_remainder.shape[0])
                resample_ratio = _adjust_ratio(occupancy)
                ratio = float(resample_ratio)
            effective_rate = float(source_sample_rate_hz) / max(ratio, 1e-6)
            if abs(effective_rate - float(sample_rate)) < 1e-3:
                resampled = y
            else:
                out_len = max(1, int(round(y.shape[0] * float(sample_rate) / max(effective_rate, 1e-6))))
                resampled = _resample_linear(y, out_len)
            merged = np.concatenate([source_remainder, resampled], axis=0) if source_remainder.size else resampled
            while merged.shape[0] >= block_size:
                _enqueue(merged[:block_size])
                merged = merged[block_size:]
            source_remainder = merged.astype(np.float32, copy=False)

    ingest_thread = threading.Thread(target=_ingest_loop, name=f"{section_name}-ingest", daemon=True)
    ingest_thread.start()

    def _run() -> None:
        nonlocal last_heartbeat_s
        nonlocal processed_cycles
        nonlocal underrun_cycles
        while not stop_event.is_set():
            device_index = resolve_output_device_index(config, section_name=section_name, logger=logger)
            chosen = next((item for item in list_output_devices() if device_index is not None and item.index == device_index), None)
            if device_index is None and sink_cfg.get("device_index") is None:
                # Use default output when no selector exists. If a selector exists and failed, retry.
                selector = sink_cfg.get("device_selector", {})
                exact_name = selector.get("exact_name") if isinstance(selector, dict) else None
                match_substring = selector.get("match_substring") if isinstance(selector, dict) else None
                selector_requested = bool(str(exact_name or "").strip()) or bool(str(match_substring or "").strip())
                if selector_requested:
                    payload = {"retry_in_ms": reconnect_delay_ms}
                    if exact_name:
                        payload["exact_name"] = str(exact_name)
                    if match_substring:
                        payload["match_substring"] = str(match_substring)
                    hostapi = selector.get("hostapi") if isinstance(selector, dict) else None
                    if hostapi:
                        payload["hostapi"] = str(hostapi)
                    logger.emit(
                        "warning",
                        module_name,
                        "device_not_found",
                        payload,
                    )
                    time.sleep(reconnect_delay_ms / 1000.0)
                    continue
            logger.emit(
                "info",
                module_name,
                "started",
                {
                    "device_index": int(device_index) if device_index is not None else None,
                    "device_name": str(chosen.name) if chosen is not None else None,
                    "device_hostapi": str(chosen.hostapi) if chosen is not None else None,
                    "channels": int(channels),
                    "sample_rate_hz": int(sample_rate),
                    "block_size": int(block_size),
                    "target_buffer_frames": int(target_buffer_frames),
                },
            )

            def _callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
                nonlocal processed_cycles
                nonlocal underrun_cycles
                nonlocal resample_ratio
                block = _consume(int(frames))
                with state_lock:
                    current_occupancy = int(buffered_frames + output_remainder.shape[0])
                if block.shape[0] != frames:
                    stats["block_size_mismatch_window"] += 1
                    stats["block_size_mismatch_total"] += 1
                if status:
                    stats["device_error_window"] += 1
                    stats["device_error_total"] += 1
                processed_cycles += 1
                if last_consume_underrun:
                    underrun_cycles += 1
                mono = block.astype(np.float32, copy=False)
                if channels == 1:
                    outdata[:, 0] = mono
                else:
                    outdata[:] = mono[:, None]
                _publish_stats(str(chosen.name) if chosen is not None else None)
                with state_lock:
                    # Update ratio from the consumer side too so occupancy reacts quickly.
                    resample_ratio = _adjust_ratio(current_occupancy)

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
                with state_lock:
                    stats["device_error_window"] += 1
                    stats["device_error_total"] += 1
                logger.emit(
                    "warning",
                    module_name,
                    "device_error",
                    {
                        "error": str(exc),
                        "device_index": int(device_index) if device_index is not None else None,
                        "retry_in_ms": reconnect_delay_ms,
                    },
                )
                time.sleep(reconnect_delay_ms / 1000.0)
                continue

            try:
                with stream:
                    while not stop_event.is_set():
                        _publish_stats(str(chosen.name) if chosen is not None else None)
                        now_s = time.time()
                        if now_s - last_heartbeat_s >= heartbeat_interval_s:
                            bus.publish(
                                "runtime.worker_loop",
                                {
                                    "t_ns": now_ns(),
                                    "module": module_name,
                                    "idle_cycles": int(underrun_cycles),
                                    "processed_cycles": int(processed_cycles),
                                    "shed_level": int(latest_shed_state.get("level", 0) or 0),
                                },
                            )
                            last_heartbeat_s = now_s
                        time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                with state_lock:
                    stats["device_error_window"] += 1
                    stats["device_error_total"] += 1
                logger.emit("warning", module_name, "device_error", {"error": str(exc), "retry_in_ms": reconnect_delay_ms})
                time.sleep(reconnect_delay_ms / 1000.0)

    thread = threading.Thread(target=_run, name=section_name, daemon=True)
    thread.start()
    return thread


def _resample_linear(samples: np.ndarray, out_len: int) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return np.zeros((out_len,), dtype=np.float32)
    if out_len <= 1 or x.shape[0] == 1:
        return np.repeat(x[:1], max(1, out_len)).astype(np.float32, copy=False)
    src_x = np.linspace(0.0, 1.0, num=x.shape[0], endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=max(1, out_len), endpoint=False)
    return np.interp(dst_x, src_x, x).astype(np.float32, copy=False)


def _sink_cfg(output_cfg: Dict[str, Any], section_name: str) -> Dict[str, Any]:
    if section_name == "host_loopback":
        cfg = output_cfg.get("host_loopback")
        if not isinstance(cfg, dict):
            cfg = output_cfg.get("virtual_mic", {})
    else:
        cfg = output_cfg.get(section_name, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _module_name(section_name: str) -> str:
    if section_name == "host_loopback":
        return "audio.output.host_loopback"
    if section_name == "usb_mic":
        return "audio.output.usb_mic"
    return "audio.output.virtual_mic"


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
