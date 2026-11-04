"""Deployment defaults survive edits/restarts and never silently apply settings."""

import tempfile
import unittest
from pathlib import Path

from remote_teleop.config import AppConfig, ConfigStore, atomic_json


class DefaultsTests(unittest.TestCase):
    def test_serializing_defaults_does_not_change_control_contract(self):
        cfg = AppConfig()
        restored = AppConfig.model_validate_json(cfg.model_dump_json())
        self.assertEqual(cfg.control_contract, restored.control_contract)
        self.assertEqual(cfg.safety.model_dump_json(), restored.safety.model_dump_json())

    def test_baseline_is_deployment_specific_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.json"
            cfg = AppConfig()
            cfg.general.robot_id = "my-deployed-robot"
            cfg.ui.port = 18812
            atomic_json(path, cfg.model_dump())
            first = ConfigStore(path)
            defaults = first.read_defaults()
            self.assertEqual(defaults["config"]["general"]["robot_id"], "my-deployed-robot")
            self.assertEqual(defaults["config"]["ui"]["port"], 18812)
            before = first.defaults_path.read_bytes()
            changed = first.read()["config"]
            changed["safety"]["watchdog_ms"] = 600
            first.save(changed, first.revision)
            second = ConfigStore(path)
            self.assertEqual(second.current.safety.watchdog_ms, 600)
            self.assertEqual(second.defaults_path.read_bytes(), before)
            self.assertEqual(second.read_defaults(), defaults)
            restored = second.save(defaults["config"], second.revision)
            self.assertTrue(restored["restart_required"])
            self.assertEqual(second.current.safety.watchdog_ms, 600)
            self.assertEqual(ConfigStore(path).current.safety.watchdog_ms, 250)

    def test_restoring_active_values_clears_restart_without_overwriting_new_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.json"
            path.write_text("{}")
            store = ConfigStore(path)
            initial = store.read()
            changed = initial["config"]
            changed["ui"]["language"] = "en"
            saved = store.save(changed, initial["revision"])
            self.assertTrue(saved["restart_required"])
            with self.assertRaisesRegex(ValueError, "CONFIG_CONFLICT"):
                store.save(store.read_defaults()["config"], initial["revision"])
            restored = store.save(store.read_defaults()["config"], saved["revision"])
            self.assertFalse(restored["restart_required"])
            self.assertEqual(restored["config"]["ui"]["language"], "zh")
            self.assertEqual(store.defaults_path.stat().st_mode & 0o777, 0o600)

    def test_each_role_keeps_its_own_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            for role, port in (("master", 18812), ("slave", 18813), ("cloud", 18814)):
                path = Path(directory) / f"{role}.json"
                cfg = AppConfig()
                cfg.ui.port = port
                atomic_json(path, cfg.model_dump())
                ConfigStore(path)
            for role, port in (("master", 18812), ("slave", 18813), ("cloud", 18814)):
                self.assertEqual(
                    ConfigStore(Path(directory) / f"{role}.json").read_defaults()["config"]["ui"]["port"],
                    port,
                )


if __name__ == "__main__":
    unittest.main()
