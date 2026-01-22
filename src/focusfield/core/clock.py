"""
CONTRACT: inline (source: src/focusfield/core/clock.md)
ROLE: Monotonic timestamps and skew helpers.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - n/a

PERF / TIMING:
  - monotonic now_ns() for all modules

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - tests/contract_tests.md must cover timestamp monotonicity

CONTRACT DETAILS (inline from src/focusfield/core/clock.md):
# Clock and timestamps

- t_ns is monotonic per stream.
- All modules must align on a shared monotonic clock.
- Convert wall time to t_ns only for logging.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
