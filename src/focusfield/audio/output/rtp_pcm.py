"""focusfield.audio.output.rtp_pcm

CONTRACT: inline
ROLE: RTP PCM output sink for remote host playout.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: audio.output.stats  Type: dict
"""

from __future__ import annotations

import os
import queue
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns


RTP_HEADER_BYTES = 12
RTP_VERSION = 2
RTP_DYNAMIC_PAYLOAD_TYPE = 96


@dataclass(frozen=True)
class ParsedRtpPcmPacket:
    seq: int
    timestamp: int
    ssrc: int
    payload_type: int
    marker: bool
    samples: np.ndarray


def encode_l16_samples(samples: np.ndarray) -> bytes:
    x = np.asarray(samples)
    if x.dtype.kind in {"i", "u"}:
        if x.dtype.kind == "u":
            max_abs = float(np.iinfo(x.dtype).max)
            x = (x.astype(np.float32) / max_abs) * 2.0 - 1.0
        else:
            max_abs = float(max(abs(np.iinfo(x.dtype).min), np.iinfo(x.dtype).max))
            x = x.astype(np.float32) / max_abs
    else:
        x = x.astype(np.float32, copy=False)
    pcm = np.clip(x.reshape(-1), -1.0, 1.0)
    return (pcm * 32767.0).astype(">i2").tobytes()


def decode_l16_samples(payload: bytes) -> np.ndarray:
    if not payload:
        return np.zeros((0,), dtype=np.float32)
    return (np.frombuffer(payload, dtype=">i2").astype(np.float32) / 32767.0).copy()


def build_rtp_packet(
    payload: bytes,
    *,
    seq: int,
    timestamp: int,
    ssrc: int,
    payload_type: int = RTP_DYNAMIC_PAYLOAD_TYPE,
    marker: bool = False,
) -> bytes:
    header = struct.pack(
        "!BBHII",
        (RTP_VERSION << 6),
        ((0x80 if marker else 0x00) | (payload_type & 0x7F)),
        int(seq) & 0xFFFF,
        int(timestamp) & 0xFFFFFFFF,
        int(ssrc) & 0xFFFFFFFF,
    )
    return header + payload


def parse_rtp_packet(packet: bytes) -> ParsedRtpPcmPacket:
    if len(packet) < RTP_HEADER_BYTES:
        raise ValueError("RTP packet too short")
    b0, b1, seq, timestamp, ssrc = struct.unpack("!BBHII", packet[:RTP_HEADER_BYTES])
    version = (b0 >> 6) & 0x03
    if version != RTP_VERSION:
        raise ValueError(f"Unsupported RTP version: {version}")
    cc = b0 & 0x0F
    if cc:
        raise ValueError("CSRC headers are not supported")
    has_extension = bool(b0 & 0x10)
    if has_extension:
        raise ValueError("RTP extensions are not supported")
    payload = packet[RTP_HEADER_BYTES:]
    if b0 & 0x20:
        if not payload:
            raise ValueError("RTP padding set with empty payload")
        pad = int(payload[-1])
        if pad <= 0 or pad > len(payload):
            raise ValueError("Invalid RTP padding")
        payload = payload[:-pad]
    return ParsedRtpPcmPacket(
        seq=int(seq),
        timestamp=int(timestamp),
        ssrc=int(ssrc),
        payload_type=int(b1 & 0x7F),
        marker=bool(b1 & 0x80),
        samples=decode_l16_samples(payload),
    )


def start_rtp_pcm_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    if str(output_cfg.get("sink", "") or "").strip().lower() not in {"rtp_pcm", "rtp"}:
        return None

    sink_cfg = output_cfg.get("rtp_pcm", {})
    if not isinstance(sink_cfg, dict):
        sink_cfg = {}
    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}

    host = str(sink_cfg.get("host") or os.environ.get("FOCUSFIELD_RTP_HOST", "")).strip()
    if not host:
        logger.emit("error", "audio.output.rtp_pcm", "config_missing", {"field": "host"})
        return None

    port = int(sink_cfg.get("port", 5004) or 5004)
    sample_rate_hz = int(sink_cfg.get("sample_rate_hz", audio_cfg.get("sample_rate_hz", 48000)) or 48000)
    packet_samples = int(sink_cfg.get("packet_samples", 960) or 960)
    payload_type = int(sink_cfg.get("payload_type", RTP_DYNAMIC_PAYLOAD_TYPE) or RTP_DYNAMIC_PAYLOAD_TYPE)
    reconnect_delay_ms = max(100, int(sink_cfg.get("reconnect_delay_ms", 750) or 750))
    send_buffer_bytes = max(65536, int(sink_cfg.get("socket_send_buffer_bytes", 262144) or 262144))
    source_topic = str(sink_cfg.get("source_topic", "audio.enhanced.final") or "audio.enhanced.final").strip()

    q_bus = bus.subscribe(source_topic)
    q_shed = bus.subscribe("runtime.shed_state")

    def _run() -> None:
        seq = random.randrange(0, 65536)
        timestamp = random.randrange(0, 2**32)
        ssrc = random.randrange(1, 2**32)
        source_remainder = np.zeros((0,), dtype=np.float32)
        latest_input_t_ns = 0
        latest_shed_state: Dict[str, Any] = {"active": False, "level": 0, "reason": "normal", "targets": []}
        last_stats_s = time.time()
        last_heartbeat_s = time.time()
        packets_sent_window = 0
        packets_sent_total = 0
        send_error_window = 0
        send_error_total = 0
        processed_cycles = 0
        sock: Optional[socket.socket] = None

        def _ensure_socket() -> socket.socket:
            nonlocal sock
            if sock is None:
                created = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                created.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer_bytes)
                created.connect((host, port))
                sock = created
                logger.emit(
                    "info",
                    "audio.output.rtp_pcm",
                    "started",
                    {
                        "host": host,
                        "port": port,
                        "packet_samples": packet_samples,
                        "sample_rate_hz": sample_rate_hz,
                        "source_topic": source_topic,
                    },
                )
            return sock

        def _publish_stats() -> None:
            nonlocal last_stats_s, packets_sent_window, send_error_window
            now_s = time.time()
            if now_s - last_stats_s < 1.0:
                return
            last_stats_s = now_s
            input_age_ms = (now_ns() - int(latest_input_t_ns)) / 1_000_000.0 if latest_input_t_ns else None
            bus.publish(
                "audio.output.stats",
                {
                    "t_ns": now_ns(),
                    "sink": "rtp_pcm",
                    "backend": "udp_rtp",
                    "device_name": f"{host}:{port}",
                    "sample_rate_hz": sample_rate_hz,
                    "source_sample_rate_hz": sample_rate_hz,
                    "occupancy_frames": int(source_remainder.shape[0]),
                    "target_buffer_frames": int(packet_samples),
                    "buffer_capacity_frames": int(packet_samples * 4),
                    "resample_ratio": 1.0,
                    "input_age_ms": float(input_age_ms) if input_age_ms is not None else None,
                    "shed_state": dict(latest_shed_state),
                    "underrun_window": 0,
                    "underrun_total": 0,
                    "overrun_window": 0,
                    "overrun_total": 0,
                    "device_error_window": int(send_error_window),
                    "device_error_total": int(send_error_total),
                    "sample_rate_mismatch_window": 0,
                    "sample_rate_mismatch_total": 0,
                    "block_size_mismatch_window": 0,
                    "block_size_mismatch_total": 0,
                    "packets_sent_window": int(packets_sent_window),
                    "packets_sent_total": int(packets_sent_total),
                    "source_topic": source_topic,
                },
            )
            packets_sent_window = 0
            send_error_window = 0

        try:
            while not stop_event.is_set():
                shed_msg = None
                try:
                    while True:
                        shed_msg = q_shed.get_nowait()
                except queue.Empty:
                    pass
                if isinstance(shed_msg, dict):
                    latest_shed_state = dict(shed_msg)

                try:
                    msg = q_bus.get(timeout=0.1)
                except queue.Empty:
                    _publish_stats()
                    now_s = time.time()
                    if now_s - last_heartbeat_s >= 1.0:
                        bus.publish(
                            "runtime.worker_loop",
                            {
                                "t_ns": now_ns(),
                                "module": "audio.output.rtp_pcm",
                                "idle_cycles": 0,
                                "processed_cycles": int(processed_cycles),
                                "shed_level": int(latest_shed_state.get("level", 0) or 0),
                            },
                        )
                        last_heartbeat_s = now_s
                    continue

                data = msg.get("data")
                if data is None:
                    continue
                x = np.asarray(data, dtype=np.float32).reshape(-1)
                msg_sample_rate_hz = int(msg.get("sample_rate_hz", sample_rate_hz) or sample_rate_hz)
                if msg_sample_rate_hz != sample_rate_hz:
                    out_len = max(1, int(round(x.shape[0] * float(sample_rate_hz) / float(msg_sample_rate_hz))))
                    x = _resample_linear(x, out_len)
                latest_input_t_ns = int(msg.get("t_ns", 0) or 0)
                merged = np.concatenate([source_remainder, x], axis=0) if source_remainder.size else x
                while merged.shape[0] >= packet_samples:
                    chunk = merged[:packet_samples]
                    merged = merged[packet_samples:]
                    packet = build_rtp_packet(
                        encode_l16_samples(chunk),
                        seq=seq,
                        timestamp=timestamp,
                        ssrc=ssrc,
                        payload_type=payload_type,
                    )
                    try:
                        _ensure_socket().send(packet)
                    except OSError as exc:
                        send_error_window += 1
                        send_error_total += 1
                        logger.emit(
                            "warning",
                            "audio.output.rtp_pcm",
                            "send_failed",
                            {"host": host, "port": port, "error": str(exc), "retry_in_ms": reconnect_delay_ms},
                        )
                        if sock is not None:
                            try:
                                sock.close()
                            except OSError:
                                pass
                            sock = None
                        time.sleep(reconnect_delay_ms / 1000.0)
                        continue
                    packets_sent_window += 1
                    packets_sent_total += 1
                    processed_cycles += 1
                    seq = (seq + 1) & 0xFFFF
                    timestamp = (timestamp + packet_samples) & 0xFFFFFFFF
                source_remainder = merged.astype(np.float32, copy=False)
                _publish_stats()
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_run, name="rtp-pcm-sink", daemon=True)
    thread.start()
    return thread


def _resample_linear(samples: np.ndarray, out_len: int) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return np.zeros((max(0, out_len),), dtype=np.float32)
    if out_len <= 1 or x.shape[0] == 1:
        return np.repeat(x[:1], max(1, out_len)).astype(np.float32, copy=False)
    src_x = np.linspace(0.0, 1.0, num=x.shape[0], endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=max(1, out_len), endpoint=False)
    return np.interp(dst_x, src_x, x).astype(np.float32, copy=False)
