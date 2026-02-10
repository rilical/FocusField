"""focusfield.audio.output.file_sink

CONTRACT: inline (source: src/focusfield/audio/output/file_sink.md)
ROLE: File sink for enhanced audio.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
  - Topic: audio.frames (optional)  Type: AudioFrame
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.file_sink.dir: base output directory (default: artifacts)
  - output.file_sink.write_raw_multich: write raw multichannel WAV

PERF / TIMING:
  - stream to disk; uses small internal buffers

FAILURE MODES:
  - write error -> log write_failed

LOG EVENTS:
  - module=audio.output.file_sink, event=write_failed, payload keys=path, error
  - module=audio.output.file_sink, event=started, payload keys=dir
"""

from __future__ import annotations

import json
import queue
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns

try:
    from focusfield.audio.devices import list_input_devices, resolve_input_device_index
except Exception:  # noqa: BLE001
    list_input_devices = None
    resolve_input_device_index = None


@dataclass
class _WavWriter:
    handle: wave.Wave_write
    channels: int
    sample_rate_hz: int


def start_file_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    sink = str(output_cfg.get("sink", ""))
    if sink.lower() not in {"file", "file_sink"}:
        return None

    file_cfg = output_cfg.get("file_sink", {})
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    base_dir = str(file_cfg.get("dir", "artifacts"))
    write_raw = bool(file_cfg.get("write_raw_multich", False))

    run_dir_cfg = config.get("runtime", {}).get("artifacts", {}).get("dir_run")
    if run_dir_cfg:
        out_dir = Path(str(run_dir_cfg)) / "audio"
    else:
        run_id = time.strftime("%Y%m%d_%H%M%S")
        out_dir = Path(base_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir.parent / "run_meta.json" if out_dir.name == "audio" else out_dir / "run_meta.json"

    q_final = bus.subscribe("audio.enhanced.final")
    q_raw = bus.subscribe("audio.frames") if write_raw else None

    def _run() -> None:
        enhanced_writer: Optional[_WavWriter] = None
        raw_writer: Optional[_WavWriter] = None
        last_raw: Optional[Dict[str, Any]] = None
        if not meta_path.exists():
            try:
                audio_device = None
                if resolve_input_device_index is not None and list_input_devices is not None:
                    try:
                        idx = resolve_input_device_index(config)
                        devices = list_input_devices()
                        audio_device = next((d for d in devices if d.index == idx), None)
                    except Exception:  # noqa: BLE001
                        audio_device = None
                meta = {
                    "t_start_ns": now_ns(),
                    "config": config,
                    "write_raw_multich": write_raw,
                    "devices": {
                        "audio": {
                            "resolved_device_index": int(audio_device.index) if audio_device is not None else None,
                            "resolved_device_name": str(audio_device.name) if audio_device is not None else None,
                            "resolved_max_input_channels": int(audio_device.max_input_channels) if audio_device is not None else None,
                        }
                    },
                }
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(meta, handle, indent=2)
            except Exception as exc:  # noqa: BLE001
                logger.emit("warning", "audio.output.file_sink", "write_failed", {"path": str(meta_path), "error": str(exc)})

        logger.emit("info", "audio.output.file_sink", "started", {"dir": str(out_dir)})

        while not stop_event.is_set():
            if q_raw is not None:
                last_raw = _drain_latest(q_raw) or last_raw
                if last_raw is not None and raw_writer is None:
                    raw_writer = _open_wav(out_dir / "raw.wav", last_raw, channels=int(last_raw.get("channels", 0) or 0))

            try:
                msg = q_final.get(timeout=0.1)
            except queue.Empty:
                continue
            if enhanced_writer is None:
                enhanced_writer = _open_wav(out_dir / "enhanced.wav", msg, channels=1)
            _write_audio(enhanced_writer, msg, logger)
            if write_raw and raw_writer is not None and last_raw is not None:
                _write_audio(raw_writer, last_raw, logger)

        _close_writer(enhanced_writer)
        _close_writer(raw_writer)

    thread = threading.Thread(target=_run, name="file-sink", daemon=True)
    thread.start()
    return thread


def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
    item = None
    try:
        while True:
            item = q.get_nowait()
    except queue.Empty:
        pass
    return item


def _open_wav(path: Path, msg: Dict[str, Any], channels: int) -> _WavWriter:
    sample_rate_hz = int(msg.get("sample_rate_hz", 48000))
    handle = wave.open(str(path), "wb")
    handle.setnchannels(max(1, int(channels)))
    handle.setsampwidth(2)  # int16
    handle.setframerate(sample_rate_hz)
    return _WavWriter(handle=handle, channels=max(1, int(channels)), sample_rate_hz=sample_rate_hz)


def _write_audio(writer: _WavWriter, msg: Dict[str, Any], logger: Any) -> None:
    try:
        data = msg.get("data")
        if data is None:
            return
        x = np.asarray(data)
        if x.dtype.kind in {"i", "u"}:
            x = x.astype(np.float32) / float(np.iinfo(x.dtype).max)
        if writer.channels == 1:
            if x.ndim > 1:
                x = x.reshape(-1)
        else:
            if x.ndim == 1:
                # Expand mono to multichannel if needed.
                x = np.repeat(x[:, None], writer.channels, axis=1)
            if x.shape[1] != writer.channels:
                return
        pcm = np.clip(x, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype(np.int16)
        writer.handle.writeframes(pcm16.tobytes())
    except Exception as exc:  # noqa: BLE001
        logger.emit("warning", "audio.output.file_sink", "write_failed", {"path": "<wav>", "error": str(exc)})


def _close_writer(writer: Optional[_WavWriter]) -> None:
    if writer is None:
        return
    try:
        writer.handle.close()
    except Exception:  # noqa: BLE001
        return
