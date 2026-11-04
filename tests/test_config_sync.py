"""Shared settings propagate together; failures preserve concurrent edits."""

import tempfile
import unittest
from pathlib import Path

from remote_teleop.config import AppConfig, ConfigStore, atomic_json
from remote_teleop.config_sync import ConfigSynchronizer, ROLES


class ConfigSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.stores = {}
        for index, role in enumerate(ROLES):
            cfg = AppConfig()
            cfg.ui.port = 18810 + index
            cfg.network.rpc_timeout_seconds = 8 + index
            cfg.security.token_env = f"TEST_{role.upper()}_TOKEN"
            path = Path(self.directory.name) / f"{role}.json"
            atomic_json(path, cfg.model_dump())
            self.stores[role] = ConfigStore(path)
        self.originals = {role: store.read() for role, store in self.stores.items()}

    async def read(self, role):
        return self.stores[role].read()

    async def save(self, role, config, revision):
        return self.stores[role].save(config, revision)

    def changed(self):
        result = self.stores["client"].read()
        result["config"]["safety"]["velocity_max"] = [1.0] * 6 + [0.16]
        result["config"]["ui"]["language"] = "en"
        return result

    async def update(self, data, save=None):
        return await ConfigSynchronizer(self.read, save or self.save).update(
            "client", data["config"], data["revision"]
        )

    async def test_sync_restore_and_local_settings_preserved(self):
        result = await self.update(self.changed())
        self.assertEqual(result["restart_targets"], list(ROLES))
        contracts = set()
        for role, store in self.stores.items():
            saved = store.read()["config"]
            self.assertEqual(saved["safety"]["velocity_max"], [1.0] * 6 + [0.16])
            self.assertEqual(saved["network"], self.originals[role]["config"]["network"])
            self.assertEqual(saved["security"], self.originals[role]["config"]["security"])
            self.assertEqual(saved["ui"]["language"], "en" if role == "client" else "zh")
            self.assertEqual(saved["ui"]["port"], self.originals[role]["config"]["ui"]["port"])
            self.assertEqual(store.current.model_dump(), self.originals[role]["config"])
            contracts.add(AppConfig.model_validate(saved).control_contract)
        self.assertEqual(len(contracts), 1)
        restored = await self.update(
            {"config": self.stores["client"].read_defaults()["config"], "revision": result["revision"]}
        )
        self.assertEqual(restored["restart_targets"], [])
        for role, store in self.stores.items():
            self.assertEqual(store.read()["config"], self.originals[role]["config"])
            self.assertEqual(store.read_defaults()["config"], self.originals[role]["config"])

    async def test_conflict_or_invalid_input_writes_nothing(self):
        data = self.changed()
        with self.assertRaisesRegex(ValueError, "CONFIG_CONFLICT"):
            await self.update({**data, "revision": "stale"})
        data["config"]["safety"]["velocity_max"] = [-1.0] * 7
        with self.assertRaises(ValueError):
            await self.update(data)
        for role, store in self.stores.items():
            self.assertEqual(store.read(), self.originals[role])

    async def test_local_edit_only_writes_selected_target(self):
        data = self.stores["client"].read()
        data["config"]["ui"]["language"] = "en"
        result = await self.update(data)
        self.assertEqual(result["restart_targets"], ["client"])
        for role in ("robot", "cloud"):
            self.assertEqual(self.stores[role].read(), self.originals[role])

    async def test_save_failure_rolls_back_earlier_roles(self):
        async def failing_save(role, config, revision):
            if role == "client":
                raise ConnectionError("offline")
            return await self.save(role, config, revision)

        with self.assertRaisesRegex(ValueError, "^CONFIG_SYNC_FAILED$"):
            await self.update(self.changed(), failing_save)
        for role, store in self.stores.items():
            self.assertEqual(store.read()["config"], self.originals[role]["config"])
            self.assertFalse(store.read()["restart_required"])

    async def test_timeout_after_commit_is_also_rolled_back(self):
        failed = False

        async def lost_reply(role, config, revision):
            nonlocal failed
            result = await self.save(role, config, revision)
            if role == "cloud" and not failed:
                failed = True
                raise TimeoutError("reply lost after save")
            return result

        with self.assertRaisesRegex(ValueError, "^CONFIG_SYNC_FAILED$"):
            await self.update(self.changed(), lost_reply)
        for role, store in self.stores.items():
            self.assertEqual(store.read()["config"], self.originals[role]["config"])

    async def test_concurrent_edit_is_never_overwritten_by_rollback(self):
        async def external_edit(role, config, revision):
            if role == "cloud":
                current = self.stores["robot"].read()
                current["config"]["ui"]["language"] = "en"
                self.stores["robot"].save(current["config"], current["revision"])
                raise ConnectionError("offline")
            return await self.save(role, config, revision)

        with self.assertRaisesRegex(ValueError, "^CONFIG_SYNC_PARTIAL$"):
            await self.update(self.changed(), external_edit)
        self.assertEqual(self.stores["robot"].read()["config"]["ui"]["language"], "en")
        self.assertEqual(self.stores["client"].read(), self.originals["client"])
        self.assertEqual(self.stores["cloud"].read(), self.originals["cloud"])


if __name__ == "__main__":
    unittest.main()
