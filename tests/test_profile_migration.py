"""Deployment migration keeps previously configured device and safety fields."""

import importlib.util
import json
from pathlib import Path
import unittest

from remote_teleop.config import AppConfig


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "normalize_profiles", ROOT / "scripts/normalize_arm_profiles.py"
)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class ProfileMigrationTests(unittest.TestCase):
    def test_site_migration_preserves_effective_fields_and_is_idempotent(self):
        raw = json.loads((ROOT / "configs/hardware.ros.example.json").read_text())
        original = AppConfig.model_validate(raw)
        migrated = MIGRATION.normalized(raw)
        self.assertEqual(migrated.robot, original.robot)
        self.assertEqual(migrated.leader, original.leader)
        self.assertEqual(migrated.safety, original.safety)
        self.assertEqual(migrated.arm_profiles.robot[0].config, original.robot)
        self.assertEqual(migrated.arm_profiles.leader[0].config, original.leader)
        self.assertEqual(MIGRATION.normalized(migrated.model_dump()), migrated)

    def test_simulated_interfaces_remain_out_of_hardware_library(self):
        migrated = MIGRATION.normalized(AppConfig().model_dump())
        self.assertEqual(migrated, AppConfig())
        self.assertEqual(migrated.arm_profiles.robot, [])
        self.assertEqual(migrated.arm_profiles.leader, [])


if __name__ == "__main__":
    unittest.main()
