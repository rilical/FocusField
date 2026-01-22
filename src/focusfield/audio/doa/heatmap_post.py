"""
CONTRACT: inline (source: src/focusfield/audio/doa/heatmap_post.md)
ROLE: Heatmap smoothing and peak picking.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
OUTPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap

CONFIG KEYS:
  - audio.doa.smoothing_alpha: temporal smoothing
  - audio.doa.top_k_peaks: peak count

PERF / TIMING:
  - per heatmap update

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - tests/audio_doa_sanity.md must cover peak stability

CONTRACT DETAILS (inline from src/focusfield/audio/doa/heatmap_post.md):
# Heatmap post-processing

- Apply smoothing and peak picking rules.
- Suppress spurious peaks with thresholding.
- Output peak list to fusion module.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
