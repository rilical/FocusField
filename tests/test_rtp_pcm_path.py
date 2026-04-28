import os
import unittest
from unittest.mock import patch

import numpy as np

from focusfield.audio.output.rtp_pcm import build_rtp_packet, encode_l16_samples, parse_rtp_packet
from focusfield.core.config import validate_config
from focusfield.tools.rtp_loopback_rx import RtpGapTracker, parse_packet_or_none


class RtpPcmPathTests(unittest.TestCase):
    def test_validate_config_rejects_missing_rtp_host_and_bad_port(self) -> None:
        cfg = {
            "output": {
                "sink": "rtp_pcm",
                "rtp_pcm": {
                    "host": "",
                    "port": 0,
                    "packet_samples": 0,
                },
            }
        }
        with patch.dict(os.environ, {}, clear=True):
            errs = validate_config(cfg)
        self.assertTrue(any("output.rtp_pcm.host" in err for err in errs))
        self.assertTrue(any("output.rtp_pcm.port" in err for err in errs))
        self.assertTrue(any("output.rtp_pcm.packet_samples" in err for err in errs))

    def test_rtp_packet_round_trip_preserves_header_and_audio(self) -> None:
        samples = np.linspace(-0.9, 0.9, 16, dtype=np.float32)
        packet = build_rtp_packet(
            encode_l16_samples(samples),
            seq=77,
            timestamp=123456,
            ssrc=991,
            payload_type=96,
            marker=True,
        )
        parsed = parse_rtp_packet(packet)
        self.assertEqual(parsed.seq, 77)
        self.assertEqual(parsed.timestamp, 123456)
        self.assertEqual(parsed.ssrc, 991)
        self.assertEqual(parsed.payload_type, 96)
        self.assertTrue(parsed.marker)
        self.assertEqual(parsed.samples.shape[0], samples.shape[0])
        np.testing.assert_allclose(parsed.samples, samples, atol=1.5 / 32767.0)

    def test_loopback_receiver_drops_malformed_udp_payloads(self) -> None:
        self.assertIsNone(parse_packet_or_none(b""))
        self.assertIsNone(parse_packet_or_none(b"not-an-rtp-packet"))

    def test_gap_tracker_resyncs_on_sender_restart(self) -> None:
        tracker = RtpGapTracker(sample_rate_hz=48000, default_frame_samples=4, max_gap_packets=3)
        first = parse_rtp_packet(
            build_rtp_packet(encode_l16_samples(np.ones(4, dtype=np.float32) * 0.25), seq=65000, timestamp=1000, ssrc=1)
        )
        out = tracker.process(first, t_ns=100)
        self.assertEqual(len(out), 1)

        restarted = parse_rtp_packet(
            build_rtp_packet(encode_l16_samples(np.ones(4, dtype=np.float32) * 0.5), seq=10, timestamp=2000, ssrc=2)
        )
        out = tracker.process(restarted, t_ns=200)

        self.assertEqual(len(out), 1)
        self.assertEqual(tracker.stale_drops, 0)
        np.testing.assert_allclose(out[0]["data"], np.ones(4, dtype=np.float32) * 0.5, atol=1.5 / 32767.0)

    def test_gap_tracker_inserts_silence_for_small_forward_gap(self) -> None:
        tracker = RtpGapTracker(sample_rate_hz=48000, default_frame_samples=4, max_gap_packets=3)
        first = parse_rtp_packet(
            build_rtp_packet(encode_l16_samples(np.ones(4, dtype=np.float32) * 0.25), seq=10, timestamp=1000, ssrc=1)
        )
        out = tracker.process(first, t_ns=100)
        self.assertEqual(len(out), 1)
        np.testing.assert_allclose(out[0]["data"], np.ones(4, dtype=np.float32) * 0.25, atol=1.5 / 32767.0)

        third = parse_rtp_packet(
            build_rtp_packet(encode_l16_samples(np.ones(4, dtype=np.float32) * 0.5), seq=12, timestamp=1008, ssrc=1)
        )
        out = tracker.process(third, t_ns=200)
        self.assertEqual(len(out), 2)
        np.testing.assert_allclose(out[0]["data"], np.zeros(4, dtype=np.float32), atol=1e-8)
        np.testing.assert_allclose(out[1]["data"], np.ones(4, dtype=np.float32) * 0.5, atol=1.5 / 32767.0)


if __name__ == "__main__":
    unittest.main()
