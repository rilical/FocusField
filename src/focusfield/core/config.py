"""
CONTRACT: inline (source: src/focusfield/core/config.md)
ROLE: Load YAML config, validate, and expose typed accessors.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - config_path: path to YAML file
  - runtime.enable_validation: enable schema validation (bool)

PERF / TIMING:
  - load once at startup

FAILURE MODES:
  - missing/invalid key -> raise error -> log validation_failed

LOG EVENTS:
  - module=core.config, event=validation_failed, payload keys=path, errors

TESTS:
  - tests/contract_tests.md must cover config validation

CONTRACT DETAILS (inline from src/focusfield/core/config.md):
# Config contract

- Config files define build mode, devices, and thresholds.
- Validation must reject missing or inconsistent fields.
- Device profiles reference known geometry and camera HFOV defaults.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
