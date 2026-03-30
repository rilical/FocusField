"""
CONTRACT: docs/11_contract_index.md
ROLE: Package marker for focusfield.bench.scenes.

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
  - module=focusfield.bench.scenes, event=not_implemented, payload keys=reason

TESTS:
  - n/a
"""

from focusfield.bench.scenes.dataset_catalog import load_dataset_catalog, normalize_dataset_catalog, validate_dataset_catalog
from focusfield.bench.scenes.labels import (
    normalize_bearing_segments,
    normalize_speaker_segments,
    validate_bearing_segments,
    validate_speaker_segments,
)
from focusfield.bench.scenes.manifest import load_scene_manifest, normalize_scene_manifest, validate_scene_manifest

__all__ = [
    "load_dataset_catalog",
    "normalize_dataset_catalog",
    "validate_dataset_catalog",
    "load_scene_manifest",
    "normalize_scene_manifest",
    "validate_scene_manifest",
    "normalize_bearing_segments",
    "normalize_speaker_segments",
    "validate_bearing_segments",
    "validate_speaker_segments",
]
