"""
CONTRACT: inline (source: src/focusfield/bench/focusbench.md)
ROLE: FocusBench CLI and report generation.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: bench.report  Type: BenchReport

CONFIG KEYS:
  - bench.output_dir: report output path
  - bench.deterministic_mode: enable deterministic replay

PERF / TIMING:
  - deterministic execution

FAILURE MODES:
  - report generation failure -> log report_failed

LOG EVENTS:
  - module=bench.focusbench, event=report_failed, payload keys=error

TESTS:
  - tests/replay_determinism.md must cover determinism

CONTRACT DETAILS (inline from src/focusfield/bench/focusbench.md):
# FocusBench

- Deterministic replay for regression testing.
- Produces BenchReport.json and plot bundle.
- Runs on recorded scenes with known ground truth.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
