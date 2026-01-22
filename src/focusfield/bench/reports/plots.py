"""
CONTRACT: inline (source: src/focusfield/bench/reports/plots.md)
ROLE: Required plot generation.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bench.plots.output_dir: plots folder

PERF / TIMING:
  - batch plot generation

FAILURE MODES:
  - plot error -> log plot_failed

LOG EVENTS:
  - module=bench.plots, event=plot_failed, payload keys=plot, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/bench/reports/plots.md):
# Required plots

- Steering MAE vs angle.
- SIR improvement vs interferer angle.
- Latency histogram.
- Target lock timeline.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
