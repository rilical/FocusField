"""
CONTRACT: inline (source: src/focusfield/bench/scenes/manifest.md)
ROLE: Bench scene format definition.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bench.scenes.manifest_path: manifest file

PERF / TIMING:
  - n/a

FAILURE MODES:
  - invalid manifest -> log manifest_invalid

LOG EVENTS:
  - module=bench.scenes.manifest, event=manifest_invalid, payload keys=path, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/bench/scenes/manifest.md):
# Bench scene manifest

## Scene object

- scene_id and description.
- audio sources:
  - file paths
  - target angle(s)
  - interferer angle(s)
  - level ratios (SNR/SIR)
- optional video:
  - recorded frames or synthetic face-bearing labels
- ground truth:
  - true target angle over time
  - true speaker timeline
- outputs:
  - required metrics to compute for this scene
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")
