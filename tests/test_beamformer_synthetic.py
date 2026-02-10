import math
import unittest

import numpy as np

from focusfield.audio.beamform.mvdr import _channel_gains  # noqa: PLC2701
from focusfield.audio.beamform.mvdr import _compute_mvdr_weights  # noqa: PLC2701


def _circular_positions(count: int, radius_m: float) -> np.ndarray:
    out = np.zeros((count, 2), dtype=np.float32)
    step = 2.0 * math.pi / float(count)
    for idx in range(count):
        ang = idx * step
        out[idx, 0] = float(radius_m * math.cos(ang))
        out[idx, 1] = float(radius_m * math.sin(ang))
    return out


class BeamformerSyntheticTests(unittest.TestCase):
    def test_channel_weights_mute_back_mics(self) -> None:
        positions = _circular_positions(7, 0.042)
        positions = np.vstack([positions, np.array([[0.0, 0.0]], dtype=np.float32)])
        # Synthetic frame with equal energy.
        x = np.ones((1024, 8), dtype=np.float32) * 0.1
        noise_rms = np.ones((8,), dtype=np.float32) * 0.01
        gains, spatial = _channel_gains(
            x,
            positions,
            target_bearing_deg=0.0,
            noise_rms=noise_rms,
            enabled=True,
            spatial_exponent=2.0,
            dead_rms_threshold=1e-6,
            min_snr_db=0.0,
            max_snr_db=30.0,
            max_clip_fraction=1.0,
        )
        self.assertEqual(gains.shape[0], 8)
        # Front mic (index 0) should be stronger than back mic (~180deg).
        back_idx = int(np.argmin(spatial[:7]))
        self.assertGreater(gains[0], gains[back_idx])

    def test_mvdr_raises_on_bad_condition(self) -> None:
        positions = _circular_positions(8, 0.08)
        sample_rate = 48000
        nfft = 512
        freq_hz = np.fft.rfftfreq(nfft, d=1.0 / sample_rate).astype(np.float32)
        f_bins = freq_hz.shape[0]
        # Singular covariance: all zeros.
        rnn = np.zeros((f_bins, 8, 8), dtype=np.complex64)
        with self.assertRaises(ValueError):
            _compute_mvdr_weights(
                positions_xy=positions,
                freq_hz=freq_hz,
                rnn=rnn,
                target_bearing_deg=0.0,
                diagonal_loading=0.0,
                max_condition_number=1e3,
            )


if __name__ == "__main__":
    unittest.main()

