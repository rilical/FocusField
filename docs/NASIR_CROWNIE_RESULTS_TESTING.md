# NASIR CROWNIE RESULTS TESTING

Date: 2026-04-13

## Summary

No real captured benchmark session exists in this repo yet.

The benchmark-complete inputs available today were synthetic fixtures used to
verify the pipeline and the reporting path.

## Smoke Fixture Run

Output packet:
- `artifacts/demo_smoke_packet/`

Verdict:
- Bench: FAIL
- Demo readiness: PASS
- Overall: FAIL

Key metrics:
- SI-SDR delta: 66.467 dB
- STOI delta: 0.000005
- Latency p50/p95/p99: 100.0 / 104.5 / 104.9 ms
- Output underrun rate: 0.0
- Queue pressure peak: 2.0

Root cause of the bad STOI result:
- The smoke fixture used an almost degenerate signal shape.
- `reference.wav` and the candidate stream were effectively the same clean tone.
- `baseline.wav` was the same tone at lower gain.
- The STOI proxy is correlation-based, so both baseline and candidate scored
  almost `1.0`, leaving a near-zero delta.
- This was a fixture problem, not a metric-code problem.

## Synthetic Benchmark Run

Output packet:
- `artifacts/demo_synth_packet/`

Verdict:
- Bench: PASS
- Demo readiness: PASS
- Overall: PASS

Key metrics:
- SI-SDR delta: 10.233 dB
- STOI delta: 0.1994
- Latency p50/p95/p99: 100.0 / 104.5 / 104.9 ms
- Output underrun rate: 0.0
- Queue pressure peak: 2.0

Interpretation:
- The end-to-end benchmark pipeline works.
- The one-shot packet generation works.
- STOI behaves sensibly once the fixture includes real degradation and recovery.

## Artifacts Produced

Synthetic PASS packet:
- `artifacts/demo_synth_packet/demo_benchmark_summary.md`
- `artifacts/demo_synth_packet/focusbench/BenchReport.json`
- `artifacts/demo_synth_packet/panel_packet/panel_scorecard.md`

Smoke FAIL packet:
- `artifacts/demo_smoke_packet/demo_benchmark_summary.md`
- `artifacts/demo_smoke_packet/focusbench/BenchReport.json`
- `artifacts/demo_smoke_packet/panel_packet/panel_scorecard.md`

## Current Limitation

The real run directories under `artifacts/20260210_125119` and
`artifacts/20260210_125212` are not benchmark-complete. They are missing the
audio and reference files required for a real A/B benchmark:

- `audio/enhanced.wav`
- real baseline WAV
- real reference WAV

## Recommendation

Capture one real same-session bundle next:
- candidate run from FocusField
- MacBook built-in baseline WAV
- close-talk reference WAV

Then run:

```bash
python3 scripts/demo_benchmark_pipeline.py \
  --candidate-run artifacts/LATEST \
  --baseline-audio /path/to/macbook_built_in.wav \
  --reference-audio /path/to/close_talk_reference.wav \
  --demo-readiness artifacts/demo/demo_readiness.json \
  --output-dir artifacts/demo/full_packet
```
