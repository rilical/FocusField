# Message contracts

## Common fields and units

- t_ns: nanoseconds, monotonic per stream.
- seq: monotonically increasing per stream.
- angles: degrees, global azimuth in [0, 360).
- scores: normalized range 0..1.

## AudioFrame

- t_ns, seq
- sample_rate_hz
- frame_samples
- channels
- data: interleaved or planar (defined in config)
- validity: frame_samples consistent with block size

## VideoFrame

- t_ns, seq
- width, height, pixel_format
- data or reference to shared buffer

## FaceTrack

- t_ns, seq
- track_id (stable across frames)
- bbox and confidence
- bearing_deg (global azimuth)
- mouth_activity (0..1)

## AssociationCandidate

- t_ns, seq
- track_id
- doa_peak_deg
- angular_distance_deg
- score_components: mouth_activity, face_confidence, doa_peak_score
- combined_score

## DoaHeatmap

- t_ns, seq
- bins (count)
- bin_size_deg
- heatmap: list of scores
- peaks: top-K (angle_deg, score)

## TargetLock

- t_ns, seq
- state: NO_LOCK, ACQUIRE, LOCKED, HOLD, HANDOFF
- mode: NO_LOCK, VISION_ONLY, AUDIO_ONLY, AV_LOCK
- target_id or target_bearing_deg
- confidence
- reason: human-readable reason string
- stability: optional jitter/hold stats

## EnhancedAudio

- t_ns, seq
- sample_rate_hz
- frame_samples
- channels (typically 1)
- stats: rms, clipping, suppression_db

## LogEvent

- t_ns, level, message
- context: module, topic, details

## TelemetrySnapshot

- t_ns, seq
- heatmap_summary (bins, peaks, confidence)
- lock_state (state, mode, target_bearing_deg, confidence, reason)
- face_summaries (track_id, bearing_deg, mouth_activity, speaking)
- health_summary (module status, degraded flags)

## BenchScene

- scene_id, description
- audio sources and labels
- ground truth for target angle and speaker timeline

## BenchReport

- report_id, config hash
- metrics table
- plot artifacts list

## Validity rules

- seq monotonic per topic.
- frame_samples consistent with declared block size.
- angles wrapped to [0, 360).

## Backpressure policy

- Each topic has a max queue depth (configurable).
- Publishers drop or throttle when queues are full.
