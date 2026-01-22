"""
CONTRACT: inline (source: src/focusfield/audio/doa/srp_phat.md)
ROLE: SRP-PHAT heatmap generation.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap

CONFIG KEYS:
  - audio.doa.bins: number of azimuth bins
  - audio.doa.update_hz: heatmap update rate
  - audio.doa.freq_band_hz: optional band
  - audio.doa.smoothing_alpha: temporal smoothing
  - audio.doa.top_k_peaks: peak count

PERF / TIMING:
  - update at configured rate; UI target >= 10 Hz

FAILURE MODES:
  - no speech or low energy -> low confidence heatmap -> log doa_low_confidence

LOG EVENTS:
  - module=audio.doa.srp_phat, event=doa_low_confidence, payload keys=confidence

TESTS:
  - tests/audio_doa_sanity.md must cover heatmap peaks

CONTRACT DETAILS (inline from src/focusfield/audio/doa/srp_phat.md):
# SRP-PHAT heatmap

## Angle bins

- Define bin size in degrees and total bin count.
- Angles wrap to [0, 360).

## Update rate

- Heatmap update_hz is configurable.
- Output DoaHeatmap at the configured rate.

## Peak finding

- Top-K peak extraction.
- Peak list includes angle_deg and score.

## Smoothing

- Optional temporal smoothing of heatmap.
- Smoothing window and alpha set in config.

## Confidence

- Confidence computed from peak-to-mean ratio or top-K spread.
- Normalized to 0..1.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
