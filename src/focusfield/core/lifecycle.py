"""
CONTRACT: inline (source: src/focusfield/core/lifecycle.md)
ROLE: Module start/stop ordering and error strategy.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - runtime.fail_fast: stop pipeline on first error (bool)

PERF / TIMING:
  - start graph ordering preserved

FAILURE MODES:
  - module start failure -> stop pipeline -> log module_failed

LOG EVENTS:
  - module=core.lifecycle, event=module_failed, payload keys=module, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/core/lifecycle.md):
# Lifecycle contract

- Define start order: config -> bus -> sensors -> processing -> UI.
- Define stop order: UI -> processing -> sensors -> bus.
- Errors must be surfaced to health and logging.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
