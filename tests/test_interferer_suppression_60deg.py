import math
import unittest
from pathlib import Path

import numpy as np

from focusfield.audio.beamform.delay_and_sum import _delay_and_sum  # noqa: PLC2701
from focusfield.audio.beamform.delay_and_sum import _shift_samples  # noqa: PLC2701


SPEED_OF_SOUND_M_S = 343.0


def _circular_positions(count: int, radius_m: float) -> np.ndarray:
    out = np.zeros((count, 2), dtype=np.float32)
    step = 2.0 * math.pi / float(count)
    for idx in range(count):
        ang = idx * step
        out[idx, 0] = float(radius_m * math.cos(ang))
        out[idx, 1] = float(radius_m * math.sin(ang))
    return out


def _sine(freq_hz: float, sample_rate_hz: int, n_samples: int, amplitude: float = 0.2) -> np.ndarray:
    t = np.arange(n_samples, dtype=np.float32) / float(sample_rate_hz)
    return (amplitude * np.sin(2.0 * np.pi * float(freq_hz) * t)).astype(np.float32)


def _plane_wave_multichannel(signal: np.ndarray, positions_xy: np.ndarray, bearing_deg: float, sample_rate_hz: int) -> np.ndarray:
    theta = np.deg2rad(float(bearing_deg))
    direction = np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)
    delays_s = (positions_xy @ direction) / SPEED_OF_SOUND_M_S
    out = np.zeros((signal.shape[0], positions_xy.shape[0]), dtype=np.float32)
    for ch in range(positions_xy.shape[0]):
        sample_shift = int(round(float(delays_s[ch]) * float(sample_rate_hz)))
        out[:, ch] = _shift_samples(signal, sample_shift)
    return out


def _power_db(signal: np.ndarray) -> float:
    x = np.asarray(signal, dtype=np.float32).reshape(-1)
    return float(10.0 * np.log10(float(np.mean(x**2)) + 1e-12))


class InterfererSuppressionTests(unittest.TestCase):
    def test_off_axis_noise_suppression_at_60deg_is_at_least_8db(self) -> None:
        sample_rate_hz = 16000
        n_samples = sample_rate_hz * 2
        target_bearing_deg = 0.0
        interferer_bearing_deg = 60.0
        suppression_threshold_db = 8.0

        positions = _circular_positions(count=8, radius_m=0.045)
        target = _sine(freq_hz=500.0, sample_rate_hz=sample_rate_hz, n_samples=n_samples, amplitude=0.20)
        interferer = _sine(freq_hz=1400.0, sample_rate_hz=sample_rate_hz, n_samples=n_samples, amplitude=0.25)

        x_target = _plane_wave_multichannel(target, positions, target_bearing_deg, sample_rate_hz)
        x_interferer = _plane_wave_multichannel(interferer, positions, interferer_bearing_deg, sample_rate_hz)
        x_mix = x_target + x_interferer

        # Input reference: omnidirectional average across microphones.
        input_mix = np.mean(x_mix, axis=1).astype(np.float32)
        input_interferer = np.mean(x_interferer, axis=1).astype(np.float32)

        # Output: delay-and-sum steered to target direction (0 deg).
        output_mix = _delay_and_sum(x_mix, positions, bearing_deg=target_bearing_deg, sample_rate=sample_rate_hz)
        output_interferer = _delay_and_sum(
            x_interferer,
            positions,
            bearing_deg=target_bearing_deg,
            sample_rate=sample_rate_hz,
        )

        input_interferer_db = _power_db(input_interferer)
        output_interferer_db = _power_db(output_interferer)
        suppression_db = input_interferer_db - output_interferer_db

        input_mix_db = _power_db(input_mix)
        output_mix_db = _power_db(output_mix)

        log_path = Path("artifacts") / "tests" / "interferer_suppression_60deg.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "test=Interferer Suppression - off-axis noise suppression >= 8 dB at 60 deg",
            f"input_interferer_db={input_interferer_db:.3f}",
            f"output_interferer_db={output_interferer_db:.3f}",
            f"suppression_db={suppression_db:.3f}",
            f"threshold_db={suppression_threshold_db:.3f}",
            f"input_mix_db={input_mix_db:.3f}",
            f"output_mix_db={output_mix_db:.3f}",
            f"pass={str(suppression_db >= suppression_threshold_db).lower()}",
        ]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.assertGreaterEqual(
            suppression_db,
            suppression_threshold_db,
            msg=(
                "Interferer suppression below requirement. "
                f"input_db={input_interferer_db:.3f}, output_db={output_interferer_db:.3f}, "
                f"suppression_db={suppression_db:.3f}, threshold_db={suppression_threshold_db:.3f}. "
                f"See log: {log_path}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
