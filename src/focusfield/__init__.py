"""
CONTRACT: docs/11_contract_index.md
ROLE: Top-level FocusField package.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - n/a

PERF / TIMING:
  - n/a

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=focusfield, event=not_implemented, payload keys=reason

TESTS:
  - n/a
"""

from .version import __version__

__all__ = ["__version__"]
