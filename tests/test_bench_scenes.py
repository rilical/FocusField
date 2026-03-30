import tempfile
import unittest
from pathlib import Path

import yaml

from focusfield.bench.scenes.dataset_catalog import load_dataset_catalog, validate_dataset_catalog
from focusfield.bench.scenes.labels import normalize_bearing_segments, normalize_speaker_segments, validate_bearing_segments
from focusfield.bench.scenes.manifest import load_scene_manifest, validate_scene_manifest


class BenchSceneContractTests(unittest.TestCase):
    def test_load_scene_manifest_normalizes_release_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "audio").mkdir()
            (root / "video").mkdir()
            (root / "audio" / "candidate.wav").write_bytes(b"")
            (root / "audio" / "baseline.wav").write_bytes(b"")
            (root / "audio" / "reference.wav").write_bytes(b"")
            (root / "audio" / "noise.wav").write_bytes(b"")
            (root / "video" / "cam0.mp4").write_bytes(b"")
            (root / "video" / "cam1.mp4").write_bytes(b"")

            manifest_path = root / "scenes.yaml"
            manifest_path.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "scenes": [
                            {
                                "scene_id": "office_handoff",
                                "description": "Recorded meeting handoff scene",
                                "audio_path": "audio/candidate.wav",
                                "reference_audio_path": "audio/reference.wav",
                                "noise_reference_audio_path": "audio/noise.wav",
                                "baseline_audio_path": "audio/baseline.wav",
                                "candidate_audio_path": "audio/candidate.wav",
                                "video_paths": ["video/cam0.mp4", "video/cam1.mp4"],
                                "start_s": 1.0,
                                "end_s": 2.5,
                                "speaker_segments": [
                                    {"start_ms": 0, "end_ms": 1200, "speaker_id": "speaker_a"},
                                ],
                                "bearing_segments": [
                                    {"start_s": 0, "end_s": 1.2, "angle_deg": 450},
                                ],
                                "required_metrics": ["si_sdr_delta_db", "handoff_latency_p95_ms"],
                                "tags": ["release", "office"],
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            loaded = load_scene_manifest(manifest_path)
            scene = loaded["scenes"][0]
            self.assertEqual(scene["scene_id"], "office_handoff")
            self.assertEqual(scene["audio_path"], str((root / "audio" / "candidate.wav").resolve()))
            self.assertEqual(scene["reference_audio_path"], str((root / "audio" / "reference.wav").resolve()))
            self.assertEqual(scene["noise_reference_audio_path"], str((root / "audio" / "noise.wav").resolve()))
            self.assertEqual(scene["baseline_audio_path"], str((root / "audio" / "baseline.wav").resolve()))
            self.assertEqual(scene["candidate_audio_path"], str((root / "audio" / "candidate.wav").resolve()))
            self.assertEqual(scene["video_paths"], [str((root / "video" / "cam0.mp4").resolve()), str((root / "video" / "cam1.mp4").resolve())])
            self.assertEqual(scene["start_s"], 1.0)
            self.assertEqual(scene["end_s"], 2.5)
            self.assertEqual(scene["speaker_segments"][0]["start_s"], 0.0)
            self.assertEqual(scene["speaker_segments"][0]["end_s"], 1.2)
            self.assertEqual(scene["bearing_segments"][0]["angle_deg"], 90.0)
            self.assertEqual(scene["required_metrics"], ["si_sdr_delta_db", "handoff_latency_p95_ms"])
            self.assertEqual(scene["tags"], ["release", "office"])
            self.assertEqual(loaded["source_path"], str(manifest_path))

    def test_validate_scene_manifest_rejects_missing_release_fields(self) -> None:
        errors = validate_scene_manifest({"scenes": [{"scene_id": "missing_fields"}]})
        self.assertTrue(any("audio_path is required" in error for error in errors))
        self.assertTrue(any("reference_audio_path is required" in error for error in errors))
        self.assertTrue(any("video_paths is required" in error for error in errors))
        self.assertTrue(any("speaker_segments is required" in error for error in errors))
        self.assertTrue(any("bearing_segments is required" in error for error in errors))
        self.assertTrue(any("tags is required" in error for error in errors))

    def test_validate_scene_manifest_reports_invalid_timeline_labels(self) -> None:
        errors = validate_scene_manifest(
            {
                "scenes": [
                    {
                        "scene_id": "bad_labels",
                        "audio_path": "audio.wav",
                        "reference_audio_path": "reference.wav",
                        "video_paths": ["cam0.mp4"],
                        "speaker_segments": [{"start": 1.0, "end": 0.5, "speaker_id": "speaker_a"}],
                        "bearing_segments": [{"start": 0.0, "end": 1.0, "angle_deg": 10.0}],
                        "tags": ["release"],
                    }
                ]
            }
        )
        self.assertTrue(any("end must be greater than start" in error for error in errors))

    def test_label_helpers_accept_aliases_and_wrap_angles(self) -> None:
        speaker_segments = normalize_speaker_segments(
            [{"start_ms": 0, "end_ms": 500, "speaker": "speaker_a", "confidence": 0.9}],
            scene_id="scene_a",
        )
        bearing_segments = normalize_bearing_segments(
            [{"start": 0.0, "end": 1.0, "angle_deg": 450.0, "confidence": 0.7}],
            scene_id="scene_a",
        )
        self.assertEqual(speaker_segments[0]["start_s"], 0.0)
        self.assertEqual(speaker_segments[0]["end_s"], 0.5)
        self.assertEqual(speaker_segments[0]["speaker_id"], "speaker_a")
        self.assertEqual(bearing_segments[0]["angle_deg"], 90.0)
        self.assertEqual(bearing_segments[0]["confidence"], 0.7)
        self.assertEqual(validate_bearing_segments(bearing_segments, scene_id="scene_a"), [])

    def test_load_dataset_catalog_indexes_datasets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "manifests").mkdir()
            (root / "clips").mkdir()
            (root / "manifests" / "office.yaml").write_text("scenes: []\n", encoding="utf-8")
            (root / "clips" / "sample.wav").write_bytes(b"")
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "datasets": [
                            {
                                "dataset_id": "office_meetings",
                                "name": "Office Meetings",
                                "version": "2025.03",
                                "license": "internal-only",
                                "usage_constraints": "release-eval-only",
                                "scene_manifests": ["manifests/office.yaml"],
                                "clip_paths": ["clips/sample.wav"],
                                "sources": [
                                    {
                                        "audio_path": "clips/sample.wav",
                                        "video_path": "clips/sample.wav",
                                    }
                                ],
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            loaded = load_dataset_catalog(catalog_path)
            dataset = loaded["datasets"][0]
            self.assertEqual(dataset["dataset_id"], "office_meetings")
            self.assertEqual(loaded["datasets_by_id"]["office_meetings"]["name"], "Office Meetings")
            self.assertEqual(dataset["scene_manifests"], [str((root / "manifests" / "office.yaml").resolve())])
            self.assertEqual(dataset["clip_paths"], [str((root / "clips" / "sample.wav").resolve())])
            self.assertEqual(dataset["sources"][0]["audio_path"], str((root / "clips" / "sample.wav").resolve()))

    def test_validate_dataset_catalog_reports_missing_required_fields(self) -> None:
        errors = validate_dataset_catalog({"datasets": [{"dataset_id": "broken"}]})
        self.assertTrue(any("version is required" in error for error in errors))
        self.assertTrue(any("license is required" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
