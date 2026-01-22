"""
CONTRACT: inline (source: src/focusfield/adapters/hw_profiles.md)
ROLE: Resolve hardware profiles for geometry and channel order.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_profile: profile name
  - configs/device_profiles.yaml: profile registry

PERF / TIMING:
  - load once at startup

FAILURE MODES:
  - unknown profile -> raise -> log profile_missing

LOG EVENTS:
  - module=adapters.hw_profiles, event=profile_missing, payload keys=profile

TESTS:
  - tests/contract_tests.md must cover profile validation

CONTRACT DETAILS (inline from src/focusfield/adapters/hw_profiles.md):
# Hardware profiles

- How to add a new mic array or camera profile.
- Required fields: geometry, channel order, HFOV.
- Validate profiles against config at startup.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
