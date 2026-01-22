"""
CONTRACT: inline (source: src/focusfield/ui/views/bench.md)
ROLE: Bench report view rendering.

INPUTS:
  - Topic: bench.report  Type: BenchReport
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - ui.views.bench.enabled: enable bench view

PERF / TIMING:
  - render on report load

FAILURE MODES:
  - render error -> log render_failed

LOG EVENTS:
  - module=ui.views.bench, event=render_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/views/bench.md):
# Bench view

- Load and display FocusBench report bundles.
- Show metrics table and required plots.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
