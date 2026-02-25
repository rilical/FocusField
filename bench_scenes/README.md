# FocusBench Scene Manifests

These manifests define standard A/B scenarios for comparing:

- `baseline` = UMA-8 DSP mode run artifacts
- `candidate` = FocusField RAW-mode run artifacts

Each scene can override `baseline_audio_path` and `candidate_audio_path`. If omitted,
the default is `<run_dir>/audio/enhanced.wav`.

Reference audio/text files are expected to be curated per scene during data collection.
