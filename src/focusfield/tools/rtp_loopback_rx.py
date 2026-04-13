from __future__ import annotations

import argparse
import signal
import socket
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from focusfield.audio.output.rtp_pcm import ParsedRtpPcmPacket, parse_rtp_packet
from focusfield.audio.output.sink import start_output_sink
from focusfield.core.bus import Bus
from focusfield.core.clock import now_ns
from focusfield.core.logging import LogEmitter


@dataclass
class RtpGapTracker:
    sample_rate_hz: int
    default_frame_samples: int
    max_gap_packets: int = 4

    def __post_init__(self) -> None:
        self._expected_seq: Optional[int] = None
        self._output_seq: int = 0
        self._gap_fills: int = 0
        self._stale_drops: int = 0

    @property
    def gap_fills(self) -> int:
        return int(self._gap_fills)

    @property
    def stale_drops(self) -> int:
        return int(self._stale_drops)

    def process(self, packet: ParsedRtpPcmPacket, *, t_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        emitted: List[Dict[str, Any]] = []
        current_t_ns = int(t_ns or now_ns())
        if self._expected_seq is None:
            emitted.append(self._build_msg(packet.samples, current_t_ns))
            self._expected_seq = (packet.seq + 1) & 0xFFFF
            return emitted

        delta = (packet.seq - self._expected_seq) & 0xFFFF
        if delta == 0:
            emitted.append(self._build_msg(packet.samples, current_t_ns))
            self._expected_seq = (packet.seq + 1) & 0xFFFF
            return emitted

        if delta < 0x8000:
            if delta <= self.max_gap_packets:
                for _ in range(delta):
                    emitted.append(self._build_msg(np.zeros((self.default_frame_samples,), dtype=np.float32), current_t_ns))
                    self._gap_fills += 1
            emitted.append(self._build_msg(packet.samples, current_t_ns))
            self._expected_seq = (packet.seq + 1) & 0xFFFF
            return emitted

        self._stale_drops += 1
        return emitted

    def _build_msg(self, samples: np.ndarray, t_ns: int) -> Dict[str, Any]:
        frame = np.asarray(samples, dtype=np.float32).reshape(-1)
        msg = {
            "t_ns": int(t_ns),
            "seq": int(self._output_seq),
            "sample_rate_hz": int(self.sample_rate_hz),
            "frame_samples": int(frame.shape[0]),
            "channels": 1,
            "data": frame,
            "stage_timestamps": {
                "network_received_t_ns": int(t_ns),
                "published_t_ns": int(t_ns),
            },
        }
        self._output_seq += 1
        return msg


def build_receiver_config(
    *,
    device_name: str,
    sample_rate_hz: int,
    packet_samples: int,
    channels: int = 2,
) -> Dict[str, Any]:
    return {
        "audio": {
            "sample_rate_hz": int(sample_rate_hz),
            "block_size": int(packet_samples),
            "agc_post": {
                "enabled": False,
            },
        },
        "output": {
            "sink": "host_loopback",
            "host_loopback": {
                "channels": int(channels),
                "buffer_blocks": 16,
                "target_buffer_blocks": 6,
                "reconnect_delay_ms": 750,
                "device_selector": {
                    "match_substring": str(device_name),
                },
            },
        },
        "trace": {
            "enabled": False,
        },
        "runtime": {
            "artifacts": {
                "dir_run": "",
            },
        },
    }


def run_receiver(
    *,
    bind_host: str,
    port: int,
    device_name: str,
    sample_rate_hz: int,
    packet_samples: int,
    channels: int,
) -> None:
    bus = Bus(max_queue_depth=64)
    logger = LogEmitter(bus, min_level="info", run_id="rtp-loopback-rx")
    stop_event = threading.Event()
    config = build_receiver_config(
        device_name=device_name,
        sample_rate_hz=sample_rate_hz,
        packet_samples=packet_samples,
        channels=channels,
    )
    sink_thread = start_output_sink(bus, config, logger, stop_event)
    if sink_thread is None:
        raise RuntimeError("Failed to start host loopback sink")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_host, int(port)))
    sock.settimeout(0.25)

    tracker = RtpGapTracker(sample_rate_hz=sample_rate_hz, default_frame_samples=packet_samples)
    packets_rx = 0
    first_peer: Optional[str] = None
    logger.emit(
        "info",
        "tools.rtp_loopback_rx",
        "listening",
        {"bind_host": bind_host, "port": int(port), "device": device_name, "sample_rate_hz": int(sample_rate_hz)},
    )

    def _handle_signal(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
        stop_event.set()

    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    try:
        while not stop_event.is_set():
            try:
                packet, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            parsed = parse_rtp_packet(packet)
            if first_peer is None:
                first_peer = f"{addr[0]}:{addr[1]}"
                logger.emit("info", "tools.rtp_loopback_rx", "peer_detected", {"peer": first_peer})
            for msg in tracker.process(parsed):
                bus.publish("audio.enhanced.final", msg)
            packets_rx += 1
            if packets_rx % 50 == 0:
                logger.emit(
                    "info",
                    "tools.rtp_loopback_rx",
                    "stream_status",
                    {
                        "packets_received": int(packets_rx),
                        "gap_fills": int(tracker.gap_fills),
                        "stale_drops": int(tracker.stale_drops),
                    },
                )
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        stop_event.set()
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive RTP PCM audio and play it into a macOS loopback device")
    parser.add_argument("--bind", default="0.0.0.0", help="Bind address for the UDP listener")
    parser.add_argument("--port", type=int, default=5004, help="UDP port to listen on")
    parser.add_argument("--device", default="Loopback Audio", help="Substring match for the target output device")
    parser.add_argument("--sample-rate-hz", type=int, default=48000, help="Expected sample rate")
    parser.add_argument("--packet-samples", type=int, default=960, help="Expected samples per packet")
    parser.add_argument("--channels", type=int, default=2, help="Output channels for the loopback device")
    args = parser.parse_args()
    run_receiver(
        bind_host=args.bind,
        port=args.port,
        device_name=args.device,
        sample_rate_hz=args.sample_rate_hz,
        packet_samples=args.packet_samples,
        channels=args.channels,
    )


if __name__ == "__main__":
    main()
