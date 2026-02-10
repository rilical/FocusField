import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from focusfield.core.artifacts import apply_retention, create_run_dir
from focusfield.core.bus import Bus
from focusfield.core.logging import LogEmitter
from focusfield.core.log_sink import start_log_sink
from focusfield.bench.replay.recorder import start_trace_recorder


class ArtifactsAndTracingTests(unittest.TestCase):
    def test_retention_keeps_last_n(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Create 12 fake run dirs with staggered mtimes.
            paths = []
            for idx in range(12):
                p = base / f"run{idx:02d}"
                p.mkdir(parents=True)
                (p / "dummy.txt").write_text("x")
                ts = time.time() - (12 - idx) * 10
                os.utime(p, (ts, ts))
                paths.append(p)
            keep = base / "run11"
            apply_retention(base, max_runs=10, keep_dir=keep)
            remaining = sorted([p.name for p in base.iterdir() if p.is_dir()])
            self.assertIn("run11", remaining)
            self.assertLessEqual(len(remaining), 10)

    def test_log_sink_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(td, run_id="test", max_runs=10)
            config = {
                "runtime": {"artifacts": {"dir_run": str(run_dir)}},
                "logging": {"file": {"enabled": True, "flush_interval_ms": 10, "rotate_mb": 0}},
            }
            bus = Bus(max_queue_depth=8)
            logger = LogEmitter(bus, min_level="error", run_id="test")
            stop = threading.Event()
            thread = start_log_sink(bus, config, logger, stop)
            self.assertIsNotNone(thread)
            logger.emit("info", "test", "hello", {"a": 1})
            time.sleep(0.05)
            stop.set()
            time.sleep(0.05)
            path = run_dir / "logs" / "events.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text().strip().splitlines()
            self.assertGreaterEqual(len(lines), 1)
            obj = json.loads(lines[-1])
            self.assertEqual(obj["context"]["event"], "hello")

    def test_trace_recorder_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(td, run_id="trace", max_runs=10)
            config = {
                "runtime": {"artifacts": {"dir_run": str(run_dir)}},
                "trace": {
                    "enabled": True,
                    "thumbnails": {"enabled": False, "fps": 1},
                    "record_raw_audio": False,
                    "record_heatmap_full": False,
                },
                "video": {"cameras": []},
            }
            bus = Bus(max_queue_depth=8)
            logger = LogEmitter(bus, min_level="error", run_id="trace")
            stop = threading.Event()
            thread = start_trace_recorder(bus, config, logger, stop)
            self.assertIsNotNone(thread)
            bus.publish("audio.vad", {"t_ns": 1, "seq": 1, "speech": False})
            bus.publish("fusion.target_lock", {"t_ns": 2, "seq": 1, "state": "NO_LOCK"})
            bus.publish("audio.beamformer.debug", {"t_ns": 3, "seq": 1, "method": "mvdr"})
            time.sleep(0.05)
            stop.set()
            time.sleep(0.05)
            self.assertTrue((run_dir / "traces" / "vad.jsonl").exists())
            self.assertTrue((run_dir / "traces" / "lock.jsonl").exists())
            self.assertTrue((run_dir / "traces" / "beamformer.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
