"""
CONTRACT: inline (source: src/focusfield/bench/metrics/metrics.md)
ROLE: Metric definitions and computation.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bench.metrics: thresholds and tolerances

PERF / TIMING:
  - batch compute after replay

FAILURE MODES:
  - metric compute error -> log metrics_failed

LOG EVENTS:
  - module=bench.metrics, event=metrics_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/bench/metrics/metrics.md):
# Metrics

- WER, delta SIR, MAE, latency definitions.
- Compute from TargetLock and ground truth.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
