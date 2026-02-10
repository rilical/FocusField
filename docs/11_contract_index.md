# FocusField contract index

This document freezes wiring, module responsibilities, and the implementation stub format so teams can build in parallel without guessing.

## Conventions

- Per-module contracts are embedded directly in each *.py file.
- The essential *.md files remain as top-level references only.
- Every implementation file uses the standard header docstring template.

Standard header template (required in each .py file):

"""
CONTRACT: inline (source: <former .md path>)
ROLE: <what this module is responsible for>

INPUTS:
  - Topic: <topic>  Type: <message>
OUTPUTS:
  - Topic: <topic>  Type: <message>

CONFIG KEYS:
  - <yaml.path>: <meaning> (units/range/default)

PERF / TIMING:
  - <update rate>, <latency budget>, <buffer size>, etc.

FAILURE MODES:
  - <what can fail> -> <what to do> -> <what to log>

LOG EVENTS:
  - module=<name>, event=<string>, payload keys=<...>

TESTS:
  - tests/<file> must cover <behavior>
"""

## Wiring spec (topics -> messages)

This is the frozen wiring table. Do not diverge from it.

| Topic | Type | Producers | Consumers | Notes |
| --- | --- | --- | --- | --- |
| audio.frames | AudioFrame | audio.capture | audio.doa.srp_phat, audio.beamform.delay_and_sum, bench.recorder | Raw multichannel audio frames |
| audio.vad | AudioVad | audio.vad | fusion.lock_state_machine, bench.recorder | Voice activity detection |
| audio.doa_heatmap | DoaHeatmap | audio.doa.srp_phat | fusion.av_association, ui.telemetry, bench.recorder | 0..360 deg likelihood over azimuth |
| vision.frames.cam0 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 0 frames (internal) |
| vision.frames.cam1 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 1 frames (internal) |
| vision.frames.cam2 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 2 frames (internal) |
| vision.face_tracks | FaceTrack[] | vision.tracking.face_track | fusion.av_association, ui.telemetry, bench.recorder | Merged tracks from all cameras |
| vision.speaker_heatmap | DoaHeatmap | vision.speaker_heatmap | ui.telemetry, bench.recorder | 0..360 deg visual activity heatmap |
| fusion.candidates | AssociationCandidate[] | fusion.av_association | fusion.lock_state_machine, fusion.target_output | Internal association candidates |
| fusion.target_lock | TargetLock | fusion.lock_state_machine | audio.beamform.delay_and_sum, ui.telemetry, bench.recorder | Target lock state machine output |
| audio.enhanced.beamformed | EnhancedAudio | audio.beamform.delay_and_sum | audio.enhance.denoise, audio.output.sink, bench.recorder | Beamformed stream |
| audio.enhanced.final | EnhancedAudio | audio.enhance.denoise, audio.enhance.agc_post | audio.output.sink, bench.recorder | Final enhanced stream |
| audio.beamformer.debug | dict | audio.beamform.mvdr, audio.beamform.delay_and_sum | ui.telemetry, bench.recorder | Debug surface: gains, refs, condition #, fallback |
| ui.telemetry | TelemetrySnapshot | ui.telemetry | ui.server, ui.views.live | UI-only aggregated snapshot |
| log.events | LogEvent | all modules | core.logging, ui.telemetry | Structured log events |
| runtime.health | dict | core.health | ui.telemetry | Health snapshot: topic staleness + drop counts |
| runtime.perf | dict | core.perf_monitor | ui.telemetry | Latency + throughput summary |
| bench.record | Tap | bench.recorder | n/a | Recorder subscribes to key topics |
| bench.report | BenchReport | bench.focusbench | ui.views.bench, user | FocusBench report bundle |

## Module contracts

### Main

src/focusfield/main/run.py

- ROLE: orchestration entrypoint; starts modules based on config and mode.
- INPUTS: config path, mode flags (mvp/full/bench/replay).
- OUTPUTS: starts tasks/threads; emits log.events for lifecycle.
- MUST: create bus, start audio capture, DOA, vision, fusion, beamform, output, UI, health monitor.

### Core runtime

src/focusfield/core/config.py

- ROLE: load YAML, validate, expose typed accessors.
- INPUTS: YAML file path.
- OUTPUTS: Config object or validation errors.
- VALIDATION: all keys referenced by modules must be validated here.

src/focusfield/core/bus.py

- ROLE: in-process pub/sub with bounded queues.
- INPUTS: publish(topic, msg).
- OUTPUTS: subscribe(topic) iterator or queue.
- REQUIREMENTS: per-topic ordering, configurable queue depth, shutdown semantics.

src/focusfield/core/clock.py

- ROLE: timestamps and skew computation.
- OUTPUTS: now_ns() monotonic; helpers for skew calculations.

src/focusfield/core/lifecycle.py

- ROLE: module start/stop ordering, error strategy.
- OUTPUTS: start graph ordering; stop ordering; error propagation rules.

src/focusfield/core/health.py

- ROLE: module heartbeat aggregation.
- INPUTS: heartbeats or log events.
- OUTPUTS: UI health snapshot with red/yellow/green state.

src/focusfield/core/artifacts.py

- ROLE: create per-run artifact folder and write run metadata.
- OUTPUTS: artifacts/<run_id>/run_meta.json, config_effective.yaml.

src/focusfield/core/log_sink.py

- ROLE: persist structured log.events to artifacts/<run_id>/logs/events.jsonl.

src/focusfield/core/perf_monitor.py

- ROLE: emit runtime.perf + persist logs/perf.jsonl.

src/focusfield/core/logging.py

- ROLE: structured logging to JSONL + console; log rotation.
- INPUTS: LogEvent objects.
- OUTPUTS: logs folder artifacts.

### Adapters

src/focusfield/adapters/audio_backend.py

- ROLE: unify audio capture APIs.
- INPUTS: device_id, channels, sample_rate_hz, block_size.
- OUTPUTS: iterator or callback for raw multichannel PCM blocks.

src/focusfield/adapters/video_backend.py

- ROLE: unify camera capture APIs.
- INPUTS: camera index, resolution, fps.
- OUTPUTS: frames with timestamps.

src/focusfield/adapters/hw_profiles.py

- ROLE: map profile name to geometry + channel map + quirks.
- INPUTS: configs/device_profiles.yaml.
- OUTPUTS: geometry struct, channel order, sample rate constraints.

### Audio pipeline

src/focusfield/audio/devices.py

- ROLE: enumerate audio devices, resolve target, verify channel count.
- INPUTS: OS device list.
- OUTPUTS: device_id + channel map + diagnostics.
- LOGS: enumeration results and chosen device.

src/focusfield/audio/capture.py

- ROLE: produce AudioFrame blocks on audio.frames.
- INPUTS: audio backend stream; config channels/sample_rate/block_size.
- OUTPUTS: Topic audio.frames (AudioFrame).
- PERF: fixed cadence, stable seq increments, minimal jitter.
- FAILURE: device disconnect -> log + reconnect or exit (config-driven).

src/focusfield/audio/preprocess.py

- ROLE: optional VAD/HPF/AGC conditioning.
- INPUTS: audio.frames.
- OUTPUTS: in-place transform or audio.frames.preprocessed (design decision must be frozen).

src/focusfield/audio/vad.py

- ROLE: voice activity detection on audio.frames.
- INPUTS: audio.frames.
- OUTPUTS: audio.vad (AudioVad).
- CONFIG: audio.vad.enabled, audio.vad.mode, audio.vad.frame_ms, audio.vad.min_speech_ratio.

src/focusfield/audio/sync/channel_order_check.py

- ROLE: verify channel mapping.
- INPUTS: test clip or live procedure.
- OUTPUTS: pass/fail + mapping suggestion; calibration artifact.

src/focusfield/audio/sync/drift_check.py

- ROLE: detect timing drift or channel desync.
- INPUTS: audio.frames.
- OUTPUTS: diagnostic stats topic or log events.

src/focusfield/audio/doa/geometry.py

- ROLE: define array geometry format and steering vector helpers.
- INPUTS: geometry from config/device profile.
- OUTPUTS: mic positions, steering delays, optional lookup tables.

src/focusfield/audio/doa/srp_phat.py

- ROLE: compute 0..360 deg heatmap.
- INPUTS: audio.frames (AudioFrame).
- OUTPUTS: audio.doa_heatmap (DoaHeatmap).
- CONFIG: doa.bin_size_deg, doa.update_hz, doa.freq_band_hz, doa.smoothing_alpha, doa.top_k_peaks.
- PERF: update at >= 10 Hz for UI; >= 5 Hz acceptable for MVP.
- FAILURE: if VAD says no speech, output low-confidence heatmap or keep running (document behavior).

src/focusfield/audio/doa/gcc_phat.py

- ROLE: baseline DOA estimator for debugging.
- INPUTS: audio.frames.
- OUTPUTS: DoaHeatmap or sparse DOA estimate.

src/focusfield/audio/doa/heatmap_post.py

- ROLE: smoothing and peak picking.
- INPUTS: audio.doa_heatmap.
- OUTPUTS: updated DoaHeatmap or separate peaks (design decision must be frozen).

src/focusfield/audio/beamform/delay_and_sum.py

- ROLE: steer beamformer to target angle.
- INPUTS: audio.frames, fusion.target_lock.
- OUTPUTS: audio.enhanced.beamformed (EnhancedAudio).
- CONFIG: beamform.use_last_lock_ms, beamform.no_lock_behavior.
- PERF: adhere to total pipeline latency budget.
- FAILURE: missing geometry -> log and fall back to omni.

src/focusfield/audio/beamform/mvdr.py

- ROLE: stretch beamformer for stronger suppression.
- INPUTS/OUTPUTS: same as delay-and-sum.
- CONFIG: enabled only if stable.

src/focusfield/audio/enhance/denoise.py

- ROLE: optional denoise stage.
- INPUTS: audio.enhanced.beamformed.
- OUTPUTS: audio.enhanced.final.
- CONFIG: denoise.enabled, denoise.backend.

src/focusfield/audio/enhance/agc_post.py

- ROLE: optional post gain stabilization.
- INPUTS: EnhancedAudio.
- OUTPUTS: EnhancedAudio.

src/focusfield/audio/output/sink.py

- ROLE: abstract output sink.
- INPUTS: audio.enhanced.final.
- OUTPUTS: routed audio to sink.

src/focusfield/audio/output/file_sink.py

- ROLE: write enhanced stream to WAV/FLAC.
- INPUTS: EnhancedAudio.
- OUTPUTS: file artifacts + metadata JSONL.

src/focusfield/audio/output/virtual_mic.py

- ROLE: placeholder for OS-specific routing; documentation only.

### Vision pipeline

src/focusfield/vision/cameras.py

- ROLE: capture frames from 1 or 3 cameras.
- OUTPUTS: vision.frames.cam0/cam1/cam2 (VideoFrame).
- FAILURE: missing camera -> mark degraded and continue.

src/focusfield/vision/tracking/face_track.py

- ROLE: face detection + tracking with stable IDs.
- INPUTS: vision.frames.*.
- OUTPUTS: vision.face_tracks batch list.
- CONTRACT: stable track_id; bbox convention must match schema.

src/focusfield/vision/tracking/track_smoothing.py

- ROLE: persistence and smoothing for tracks.
- INPUTS: raw detections.
- OUTPUTS: stabilized tracks.
- CONFIG: max missing frames, smoothing alpha.

src/focusfield/vision/mouth/mouth_activity.py

- ROLE: compute mouth activity scalar.
- INPUTS: face landmarks or mouth ROI per track.
- OUTPUTS: mouth_activity embedded in FaceTrack.

src/focusfield/vision/mouth/thresholds.py

- ROLE: define speaking boolean from mouth_activity (hysteresis).
- INPUTS: mouth_activity time series.
- OUTPUTS: speaking flag embedded or separate.
- CONFIG: on/off thresholds + min frames.

src/focusfield/vision/speaker_heatmap.py

- ROLE: visual activity heatmap from face tracks.
- INPUTS: vision.face_tracks.
- OUTPUTS: vision.speaker_heatmap (DoaHeatmap).
- CONFIG: vision.heatmap.bin_size_deg, vision.heatmap.sigma_deg, vision.heatmap.top_k_peaks, vision.heatmap.smoothing_alpha.

src/focusfield/vision/calibration/bearing.py

- ROLE: pixel x -> camera bearing -> global azimuth.
- INPUTS: FaceTrack bbox center x, camera hfov, yaw offset.
- OUTPUTS: FaceTrack bearing_deg (global).

src/focusfield/vision/calibration/hfov_estimation.py

- ROLE: helper to estimate HFOV.
- OUTPUTS: calibration artifact file.

src/focusfield/vision/fusion_helpers.py

- ROLE: shared utilities for fusion (angle wrap, nearest neighbor, scoring helpers).

### Fusion

src/focusfield/fusion/av_association.py

- ROLE: associate DOA peaks with face tracks.
- INPUTS: audio.doa_heatmap, vision.face_tracks.
- OUTPUTS: fusion.candidates (AssociationCandidate[]).
- CONFIG: fusion.max_assoc_deg, score weights.

src/focusfield/fusion/confidence.py

- ROLE: combine score components into a confidence scalar.
- INPUTS: candidate components.
- OUTPUTS: confidence scalar + debug breakdown.

src/focusfield/fusion/lock_state_machine.py

- ROLE: stable target selection with hysteresis.
- INPUTS: fusion.candidates (+ optional raw DOA peaks).
- OUTPUTS: fusion.target_lock (TargetLock).
- CONFIG: acquire_threshold, hold_ms, handoff_min_ms, drop_threshold.

src/focusfield/fusion/target_output.py

- ROLE: normalize TargetLock output, fill reason strings, compute stability stats.
- OUTPUTS: TargetLock with reason and debug data.

### UI

src/focusfield/ui/telemetry.py

- ROLE: define UI telemetry messages (compact, not raw audio).
- INPUTS: audio.doa_heatmap, vision.speaker_heatmap, TargetLock, FaceTrack summaries.
- OUTPUTS: ui.telemetry topic and websocket payloads.

src/focusfield/ui/server.py

- ROLE: serve live dashboard + websocket.
- INPUTS: ui.telemetry.
- OUTPUTS: UI at localhost and /health endpoint.

src/focusfield/ui/views/live.py

- ROLE: live dashboard view spec.
- INPUTS: ui.telemetry.
- OUTPUTS: rendered live view.

src/focusfield/ui/views/bench.py

- ROLE: FocusBench report viewer spec.
- INPUTS: bench.report.
- OUTPUTS: rendered bench view.

src/focusfield/ui/assets/ui_contract.py

- ROLE: placeholder for UI asset naming conventions.

### Bench / Replay

src/focusfield/bench/focusbench.py

- ROLE: FocusBench modes, CLI flags, output folder structure.
- OUTPUTS: report folder with required artifacts.

src/focusfield/bench/replay/recorder.py

- ROLE: tap live pipeline topics and record a session.
- INPUTS: audio.frames, audio.vad, vision.face_tracks, audio.doa_heatmap, vision.speaker_heatmap, fusion.target_lock, audio.enhanced.final.
- OUTPUTS: bench scene folder (audio_raw.wav, enhanced.wav, tracks.jsonl, doa.jsonl, visual_heatmap.jsonl, vad.jsonl, lock.jsonl, scene.json).

src/focusfield/bench/replay/player.py

- ROLE: replay recorded scenes deterministically.
- INPUTS: bench scene folder.
- OUTPUTS: same topics as live capture.

src/focusfield/bench/scenes/manifest.py

- ROLE: define bench scene format and required fields.

src/focusfield/bench/scenes/labels.py

- ROLE: define ground-truth speaker timeline format.

src/focusfield/bench/scenes/dataset_catalog.py

- ROLE: list dataset sources and licensing notes.

src/focusfield/bench/metrics/metrics.py

- ROLE: metric definitions (DOA MAE, target accuracy, delta SIR, WER, latency, dropouts).

src/focusfield/bench/metrics/scoring.py

- ROLE: convert metrics to pass/fail thresholds.

src/focusfield/bench/reports/report_schema.py

- ROLE: report JSON structure and required fields.

src/focusfield/bench/reports/plots.py

- ROLE: required plots list and naming conventions.
