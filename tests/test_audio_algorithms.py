import unittest
from unittest.mock import patch

import numpy as np

from focusfield.audio.doa.srp_phat import SrpPhatDoa
from focusfield.audio.enhance.denoise import _RnnoiseState  # noqa: PLC2701
from focusfield.audio.enhance.denoise import _rnnoise_like_denoise  # noqa: PLC2701


class AudioAlgorithmTests(unittest.TestCase):
    def test_doa_peak_continuity_boosts_previous_peak_region(self) -> None:
        config = {
            "audio": {
                "sample_rate_hz": 16000,
                "block_size": 512,
                "doa": {"bins": 72, "update_hz": 10},
            }
        }
        with patch(
            "focusfield.audio.doa.srp_phat.load_mic_positions",
            return_value=([(0.0, 0.0), (0.04, 0.0)], [0, 1]),
        ):
            estimator = SrpPhatDoa(config)
        estimator._prev_peak_idx = 10  # noqa: SLF001
        scores = np.zeros((72,), dtype=np.float32)
        scores[30] = 1.0
        boosted = estimator._apply_peak_continuity(scores)  # noqa: SLF001
        self.assertGreater(float(boosted[10]), float(scores[10]))

    def test_rnnoise_like_backend_suppresses_energy(self) -> None:
        state = _RnnoiseState(nfft=512)
        x = (np.random.randn(400).astype(np.float32) * 0.08)
        # Prime noise estimate on non-speech blocks.
        for _ in range(3):
            _rnnoise_like_denoise(
                x,
                state,
                speech=False,
                noise_ema_alpha=0.98,
                gain_ema_alpha=0.85,
                strength=0.8,
                min_gain=0.05,
            )
        y = _rnnoise_like_denoise(
            x,
            state,
            speech=True,
            noise_ema_alpha=0.98,
            gain_ema_alpha=0.85,
            strength=0.8,
            min_gain=0.05,
        )
        self.assertEqual(y.shape, x.shape)
        self.assertLessEqual(float(np.mean(y**2)), float(np.mean(x**2)) + 1e-6)


if __name__ == "__main__":
    unittest.main()
