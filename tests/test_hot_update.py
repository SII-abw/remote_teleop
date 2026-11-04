"""Live safety snapshots, recovery from partial application, and motion gates."""

import unittest

from remote_teleop.config import AppConfig
from remote_teleop.config_sync import ConfigSynchronizer
from remote_teleop.gateway import live_contracts_match
import test_config_sync as sync_tests
from remote_teleop.control import Controller
from test_core import Clock


class HotUpdateTests(unittest.IsolatedAsyncioTestCase):
    setUp = sync_tests.ConfigSyncTests.setUp
    read = sync_tests.ConfigSyncTests.read
    save = sync_tests.ConfigSyncTests.save
    changed = sync_tests.ConfigSyncTests.changed

    async def apply(self, role, revision):
        return self.stores[role].apply_safety(revision)

    async def live_update(self, data, apply=None):
        return await ConfigSynchronizer(self.read, self.save, apply or self.apply).update(
            "client", data["config"], data["revision"]
        )

    async def test_live_safety_and_cold_edit_are_distinguished(self):
        refs = {role: store.current for role, store in self.stores.items()}
        data = self.changed()
        data["config"]["safety"]["acceleration_max"] = [2.0] * 6 + [0.4]
        data["config"]["safety"]["checks"]["tracking_error"] = False
        result = await self.live_update(data)
        self.assertTrue(result["hot_applied"])
        self.assertEqual(result["restart_targets"], ["client"])
        for role, store in self.stores.items():
            self.assertIs(store.current, refs[role])
            self.assertEqual(store.current.safety.model_dump(), data["config"]["safety"])
            self.assertEqual(store.current.ui.language, "zh")
            self.assertFalse(store.read()["hot_update_pending"])
            self.assertEqual(store.read_defaults()["config"], self.originals[role]["config"])

    async def test_partial_application_is_reported_and_retry_repairs_without_restart(self):
        async def failing_apply(role, revision):
            if role == "cloud":
                raise ConnectionError("connection lost")
            return await self.apply(role, revision)

        data = self.changed()
        with self.assertRaisesRegex(ValueError, "CONFIG_APPLY_PENDING"):
            await self.live_update(data, failing_apply)
        self.assertFalse(
            live_contracts_match(
                self.stores["cloud"].current,
                {"config_contract": self.stores["robot"].current.control_contract},
                self.stores["client"].current.control_contract,
            )
        )
        result = await self.live_update(self.stores["client"].read())
        self.assertTrue(result["hot_applied"])
        self.assertEqual(len({store.current.control_contract for store in self.stores.values()}), 1)

    async def test_invalid_live_combination_writes_nothing(self):
        data = self.stores["client"].read()
        data["config"]["recording"]["fps"] = 20
        data["config"]["safety"]["control_hz"] = 20
        # The saved combination is valid; active recording.fps is still 60.
        AppConfig.model_validate(data["config"])
        with self.assertRaises(ValueError):
            await self.live_update(data)
        for role, store in self.stores.items():
            self.assertEqual(store.read(), self.originals[role])

    async def test_stale_apply_does_not_mutate_live_snapshot(self):
        store = self.stores["robot"]
        data = store.read()
        data["config"]["safety"]["velocity_max"] = [1.0] * 7
        store.save(data["config"], data["revision"])
        with self.assertRaisesRegex(ValueError, "CONFIG_CONFLICT"):
            store.apply_safety(data["revision"])
        self.assertEqual(store.current.model_dump(), self.originals["robot"]["config"])

    def test_safety_mismatch_can_connect_but_cannot_move(self):
        old = AppConfig()
        new = old.model_copy(deep=True)
        new.safety.checks.velocity_limits = False
        self.assertEqual(old.connection_contract, new.connection_contract)
        self.assertNotEqual(old.control_contract, new.control_contract)
        self.assertFalse(
            live_contracts_match(new, {"config_contract": old.control_contract}, new.control_contract)
        )
        self.assertFalse(live_contracts_match(new, {}, new.control_contract))
        self.assertTrue(
            live_contracts_match(new, {"config_contract": new.control_contract}, new.control_contract)
        )
        new.general.operation_mode = "teleop"
        self.assertNotEqual(old.connection_contract, new.connection_contract)

    async def test_existing_controller_uses_new_velocity_and_acceleration(self):
        store = self.stores["robot"]
        clock = Clock()
        controller = Controller(store.current, clock)
        data = self.stores["client"].read()
        data["config"]["safety"]["velocity_max"] = [0.1] * 7
        data["config"]["safety"]["acceleration_max"] = [0.2] * 7
        await self.live_update(data)
        q = [0.0] * 14
        controller.recording = True
        controller.heartbeat(True)
        controller.request("arm", q, True)
        controller.request("start", q, True)
        controller.mode_ready(controller.epoch, q)
        previous_velocity = 0.0
        for index in range(100):
            clock.step()
            controller.heartbeat(True)
            controller.offer(
                {
                    "type": "policy_action",
                    "epoch": controller.epoch,
                    "seq": index,
                    "observation_id": controller.observe(),
                    "action": [1.0] + [0.0] * 13,
                },
                "robot",
            )
            action = controller.tick(q, True).action
            velocity = (action[0] - q[0]) / 0.01
            self.assertLessEqual(abs(velocity), 0.100001)
            self.assertLessEqual(abs(velocity - previous_velocity) / 0.01, 0.200001)
            q, previous_velocity = action, velocity
        self.assertGreater(previous_velocity, 0.09)


if __name__ == "__main__":
    unittest.main()
