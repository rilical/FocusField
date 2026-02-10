"""focusfield.bench.replay.recorder

CONTRACT: inline (source: src/focusfield/bench/replay/recorder.md)
ROLE: Record live pipeline topics into a debuggable trace bundle.

This implementation is intentionally lightweight for Raspberry Pi:
  - JSONL traces for key topics (peaks, lock, VAD, beam stats)
  - Optional WAV recording (raw multichannel + enhanced mono)
  - Optional 1fps thumbnails per camera

Artifacts are written under `runtime.artifacts.dir_run`:
  - traces/*.jsonl
  - thumbs/*.jpg
  - audio/*.wav (if enabled)

CONFIG KEYS:
  - trace.enabled
  - trace.level: low|medium|high (currently affects defaults only)
  - trace.thumbnails.enabled
  - trace.thumbnails.fps
  - trace.record_raw_audio
  - trace.record_heatmap_full
  - runtime.artifacts.dir_run
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


@dataclass
class _WavWriter:
    handle: wave.Wave_write
    channels: int
    sample_rate_hz: int


def start_trace_recorder(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    trace_cfg = config.get("trace", {})
    if not isinstance(trace_cfg, dict):
        trace_cfg = {}
    if not bool(trace_cfg.get("enabled", True)):
        return None

    run_dir = config.get("runtime", {}).get("artifacts", {}).get("dir_run")
    if not run_dir:
        logger.emit("warning", "bench.recorder", "record_failed", {"error": "runtime.artifacts.dir_run_missing"})
        return None
    run_path = Path(str(run_dir))
    traces_dir = run_path / "traces"
    thumbs_dir = run_path / "thumbs"
    audio_dir = run_path / "audio"
    traces_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    thumbnails_cfg = trace_cfg.get("thumbnails", {})
    if not isinstance(thumbnails_cfg, dict):
        thumbnails_cfg = {}
    thumbs_enabled = bool(thumbnails_cfg.get("enabled", True))
    thumbs_fps = float(thumbnails_cfg.get("fps", 1.0))
    thumbs_period_s = 1.0 / max(0.1, thumbs_fps) if thumbs_enabled else 0.0

    record_raw = bool(trace_cfg.get("record_raw_audio", True))
    record_heatmap_full = bool(trace_cfg.get("record_heatmap_full", False))

    q_vad = bus.subscribe("audio.vad")
    q_doa = bus.subscribe("audio.doa_heatmap")
    q_faces = bus.subscribe("vision.face_tracks")
    q_lock = bus.subscribe("fusion.target_lock")
    q_beam = bus.subscribe("audio.beamformer.debug")
    q_final = bus.subscribe("audio.enhanced.final")
    q_raw = bus.subscribe("audio.frames") if record_raw else None

    cameras = [cam.get("id", f"cam{idx}") for idx, cam in enumerate(config.get("video", {}).get("cameras", []))]
    q_frames = {cam_id: bus.subscribe(f"vision.frames.{cam_id}") for cam_id in cameras}

    def _open_trace(name: str):
        return open(traces_dir / name, "a", encoding="utf-8")

    def _write_jsonl(fh, obj: Dict[str, Any]) -> None:
        fh.write(json.dumps(obj, sort_keys=True) + "\n")

    def _maybe_fsync(fh) -> None:
        try:
            fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def _drain_latest(q: queue.Queue) -> Optional[Any]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _open_wav(path: Path, channels: int, sample_rate_hz: int) -> _WavWriter:
        handle = wave.open(str(path), "wb")
        handle.setnchannels(int(channels))
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate_hz))
        return _WavWriter(handle=handle, channels=int(channels), sample_rate_hz=int(sample_rate_hz))

    def _write_wav(writer: _WavWriter, data: np.ndarray) -> None:
        x = np.asarray(data)
        if x.dtype.kind in {"i", "u"}:
            x = x.astype(np.float32) / float(np.iinfo(x.dtype).max)
        if writer.channels == 1:
            x = x.reshape(-1)
        else:
            if x.ndim == 1:
                x = np.repeat(x[:, None], writer.channels, axis=1)
        pcm = np.clip(x, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype(np.int16)
        writer.handle.writeframes(pcm16.tobytes())

    def _run() -> None:
        vad_fh = _open_trace("vad.jsonl")
        doa_fh = _open_trace("doa.jsonl")
        faces_fh = _open_trace("faces.jsonl")
        lock_fh = _open_trace("lock.jsonl")
        beam_fh = _open_trace("beamformer.jsonl")

        enhanced_writer: Optional[_WavWriter] = None
        raw_writer: Optional[_WavWriter] = None
        last_thumb_s: Dict[str, float] = {cam_id: 0.0 for cam_id in cameras}
        thumb_seq: Dict[str, int] = {cam_id: 0 for cam_id in cameras}
        last_raw: Optional[Dict[str, Any]] = None

        next_fsync_s = time.time() + 0.5
        try:
            logger.emit("info", "bench.recorder", "started", {"dir": str(run_path)})
            while not stop_event.is_set():
                vad = _drain_latest(q_vad)
                if vad is not None:
                    _write_jsonl(vad_fh, vad)
                doa = _drain_latest(q_doa)
                if doa is not None:
                    if not record_heatmap_full:
                        doa = {
                            "t_ns": doa.get("t_ns", now_ns()),
                            "seq": doa.get("seq"),
                            "bins": doa.get("bins"),
                            "bin_size_deg": doa.get("bin_size_deg"),
                            "peaks": doa.get("peaks", []),
                            "confidence": doa.get("confidence", 0.0),
                        }
                    _write_jsonl(doa_fh, doa)
                faces = _drain_latest(q_faces)
                if faces is not None:
                    _write_jsonl(faces_fh, {"t_ns": now_ns(), "faces": faces})
                lock_msg = _drain_latest(q_lock)
                if lock_msg is not None:
                    _write_jsonl(lock_fh, lock_msg)
                beam_msg = _drain_latest(q_beam)
                if beam_msg is not None:
                    _write_jsonl(beam_fh, beam_msg)

                if q_raw is not None:
                    last_raw = _drain_latest(q_raw) or last_raw
                    if last_raw is not None and raw_writer is None:
                        ch = int(last_raw.get("channels", 0) or 0)
                        sr = int(last_raw.get("sample_rate_hz", 48000))
                        if ch > 0:
                            raw_writer = _open_wav(audio_dir / "raw.wav", channels=ch, sample_rate_hz=sr)
                final_msg = _drain_latest(q_final)
                if final_msg is not None:
                    if enhanced_writer is None:
                        sr = int(final_msg.get("sample_rate_hz", 48000))
                        enhanced_writer = _open_wav(audio_dir / "enhanced.wav", channels=1, sample_rate_hz=sr)
                    data = final_msg.get("data")
                    if data is not None:
                        _write_wav(enhanced_writer, np.asarray(data))
                        if raw_writer is not None and last_raw is not None:
                            raw_data = last_raw.get("data")
                            if raw_data is not None:
                                _write_wav(raw_writer, np.asarray(raw_data))

                if thumbs_enabled and cameras:
                    now_s = time.time()
                    for cam_id, q in q_frames.items():
                        frame_msg = _drain_latest(q)
                        if frame_msg is None:
                            continue
                        if thumbs_period_s > 0 and (now_s - last_thumb_s.get(cam_id, 0.0)) < thumbs_period_s:
                            continue
                        frame = frame_msg.get("data")
                        if frame is None:
                            continue
                        try:
                            import cv2

                            ok, encoded = cv2.imencode(".jpg", frame)
                            if not ok:
                                continue
                            thumb_seq[cam_id] = int(thumb_seq.get(cam_id, 0)) + 1
                            out = thumbs_dir / f"{cam_id}_{thumb_seq[cam_id]:06d}.jpg"
                            with open(out, "wb") as handle:
                                handle.write(encoded.tobytes())
                            last_thumb_s[cam_id] = now_s
                        except Exception:  # noqa: BLE001
                            continue

                now_s = time.time()
                if now_s >= next_fsync_s:
                    next_fsync_s = now_s + 0.5
                    for fh in (vad_fh, doa_fh, faces_fh, lock_fh, beam_fh):
                        _maybe_fsync(fh)
                    if enhanced_writer is not None:
                        try:
                            enhanced_writer.handle.flush()
                        except Exception:
                            pass
                    if raw_writer is not None:
                        try:
                            raw_writer.handle.flush()
                        except Exception:
                            pass
                time.sleep(0.01)
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "bench.recorder", "record_failed", {"error": str(exc)})
        finally:
            for fh in (vad_fh, doa_fh, faces_fh, lock_fh, beam_fh):
                try:
                    fh.close()
                except Exception:
                    pass
            for writer in (enhanced_writer, raw_writer):
                if writer is None:
                    continue
                try:
                    writer.handle.close()
                except Exception:
                    pass

    thread = threading.Thread(target=_run, name="trace-recorder", daemon=True)
    thread.start()
    return thread
