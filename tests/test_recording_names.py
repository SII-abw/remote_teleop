"""Read-only name preflight and strict recording collision regression."""
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from remote_teleop.config import AppConfig
from remote_teleop.recording import Recorder
from remote_teleop.runtime import RobotRuntime


class RecordingNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = AppConfig()
        self.cfg.recording.root = str(Path(self.tmp.name) / "data")
        self.recorder = Recorder(self.cfg)
        self.root = Path(self.cfg.recording.root)

    def test_preflight_has_no_creation_or_recorder_side_effect(self):
        result = self.recorder.name_status("session")
        self.assertTrue(result["available"])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.recorder.status()["state"], "IDLE")
        runtime = object.__new__(RobotRuntime)
        runtime.recorder = self.recorder
        self.assertEqual(runtime._operation("record_name", {"name": "session"}), result)

    def test_duplicate_preserves_saved_bytes_and_suggests_next_name(self):
        saved = self.root / "foldbox_hil_20261026"
        saved.mkdir(parents=True)
        marker = saved / "keep.parquet"
        marker.write_bytes(b"existing robot episode")
        (self.root / "foldbox_hil_20261026_001").mkdir()
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.recorder.name_status(saved.name)
        self.assertEqual(result["error"], "DATASET_EXISTS")
        self.assertEqual(result["suggested_name"], "foldbox_hil_20261026_002")
        self.assertFalse(result["available"])
        with self.assertRaisesRegex(ValueError, "DATASET_EXISTS"):
            self.recorder.start(saved.name, {})
        self.assertEqual(marker.read_bytes(), b"existing robot episode")
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_numbered_recordings_keep_the_original_base(self):
        (self.root / "foldbox_20261026_001").mkdir(parents=True)
        self.assertEqual(self.recorder.name_status("foldbox_20261026_001")["suggested_name"], "foldbox_20261026_002")

    def test_invalid_names_cannot_escape_root(self):
        for name in ("", "..", "../outside", "x/y", "x..y", "a"*81, None):
            result = self.recorder.name_status(name)
            self.assertFalse(result["valid"])
            self.assertEqual(result["error"], "INVALID_DATASET_NAME")
            self.assertEqual(result["suggested_name"], "episode_001")
        self.assertFalse(self.root.exists())

    def test_suggestions_fit_the_name_limit(self):
        self.root.mkdir()
        name = "a"*80
        (self.root/name).mkdir()
        suggestion = self.recorder.name_status(name)["suggested_name"]
        self.assertEqual(len(suggestion), 80)
        self.assertTrue(self.recorder.name_status(suggestion)["available"])

    def test_dangling_symlink_is_occupied(self):
        self.root.mkdir()
        (self.root/"session").symlink_to(self.root/"not-created")
        self.assertFalse(self.recorder.name_status("session")["available"])
        with self.assertRaisesRegex(ValueError, "DATASET_EXISTS"):
            self.recorder.start("session", {})

    def test_start_rechecks_after_available_preflight(self):
        self.assertTrue(self.recorder.name_status("race")["available"])
        (self.root/"race").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "DATASET_EXISTS"):
            self.recorder.start("race", {})

    def test_suggested_name_can_start_with_fake_writer(self):
        (self.root/"session").mkdir(parents=True)
        name = self.recorder.name_status("session")["suggested_name"]
        class Dataset:
            @classmethod
            def create(cls, *args, root, **kwargs):
                Path(root).mkdir()
                return cls()
            def clear_episode_buffer(self): pass
            def finalize(self): pass
            def stop_image_writer(self): pass
        fake = ModuleType("remote_teleop.components.datasets.lerobot_dataset")
        fake.LeRobotDataset = Dataset
        with patch.dict("sys.modules", {fake.__name__: fake}):
            self.recorder.start(name, {})
            self.assertEqual(self.recorder.state, "RECORDING")
            self.recorder.finish("discard")
        self.assertEqual(self.recorder.state, "IDLE")
        outcome = json.loads((self.root/name/"audit/outcome.json").read_text())
        self.assertEqual(outcome["outcome"], "discard")
        self.assertEqual(outcome["frames"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
