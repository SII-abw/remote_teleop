"""Deterministic safety, data contract and authentication regression tests."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from remote_teleop.config import AppConfig, ConfigStore, atomic_json
from remote_teleop.control import Controller, Mode, Rejected
from remote_teleop.datasets import DatasetInspector, hil_features
from remote_teleop.protocol import Outbox, decode, encode
from remote_teleop.security import Credentials, create_credentials


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def step(self, seconds=0.01):
        self.now += seconds


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.clock = Clock()
        self.c = Controller(self.cfg, self.clock)
        self.q = [0.0] * 14
        self.c.recording = True
        self.c.heartbeat(True)

    def activate(self):
        self.c.request("arm", self.q, True)
        self.c.request("start", self.q, True)
        self.c.mode_ready(self.c.epoch, self.q)
        self.offer()
        self.assertEqual(self.c.mode, Mode.POLICY)

    def packet(self, human=False):
        return dict(
            type="human_action" if human else "policy_action",
            epoch=self.c.epoch,
            seq=self.c.last_seq + 1,
            observation_id=self.c.observe(),
            action=self.q.copy(),
        )

    def offer(self, human=False):
        packet = self.packet(human)
        self.c.offer(packet, "client" if human else "robot")
        return packet

    def test_hil_switch_invalidates_previous_authority_and_preserves_release_frame(self):
        self.activate()
        self.assertEqual(self.c.tick(self.q, True).hil_state, 0)
        old = self.packet()
        self.c.request("toggle", self.q, True)
        with self.assertRaisesRegex(Rejected, "STALE_EPOCH"):
            self.c.offer(old, "robot")
        self.c.mode_ready(self.c.epoch, self.q)
        self.offer(True)
        human = self.c.tick(self.q, True)
        self.assertEqual((human.hil_state, human.intervention, human.source), (1, 1, "human"))
        self.assertEqual(human.policy_action, [0] * 14)
        self.c.request("toggle", self.q, True)
        with self.assertRaisesRegex(Rejected, "CONTROL_NOT_GRANTED"):
            self.offer()
        self.c.mode_ready(self.c.epoch, self.q)
        self.offer()
        for _ in range(3):
            self.assertEqual(self.c.tick(self.q, True).hil_state, 2)
        self.c.release_frame = False  # consumed only by successful recorder enqueue
        self.assertEqual(self.c.tick(self.q, True).hil_state, 0)

    def test_estop_latches_and_never_commands_home(self):
        self.activate()
        self.c.request("estop", self.q, False)
        self.assertIsNone(self.c.tick(self.q, True))
        self.assertEqual(self.c.mode, Mode.ESTOP)
        for op in ["arm", "start", "disarm"]:
            with self.assertRaises(Rejected):
                self.c.request(op, self.q, True)
        with self.assertRaisesRegex(Rejected, "CONTROL_NOT_GRANTED"):
            self.offer()
        with self.assertRaisesRegex(Rejected, "HEALTH_CHECK_FAILED"):
            self.c.request("reset", self.q, False)
        self.c.request("reset", self.q, True)
        self.assertEqual(self.c.mode, Mode.DISARMED)

    def test_source_sequence_observation_and_numeric_validation(self):
        self.activate()
        packet = self.packet()
        with self.assertRaisesRegex(Rejected, "WRONG_SOURCE"):
            self.c.offer(packet, "cloud")
        self.c.offer(packet, "robot")
        with self.assertRaisesRegex(Rejected, "STALE_SEQUENCE"):
            self.c.offer(packet, "robot")
        packet = self.packet()
        self.clock.step(1.01)
        with self.assertRaisesRegex(Rejected, "STALE_OBSERVATION"):
            self.c.offer(packet, "robot")
        for invalid in [True, float("nan"), float("inf"), "1"]:
            packet = self.packet()
            packet["action"][0] = invalid
            with self.assertRaises(Rejected):
                self.c.offer(packet, "robot")
        packet = self.packet()
        packet["action"][0] = 100
        with self.assertRaisesRegex(Rejected, "POSITION_LIMIT"):
            self.c.offer(packet, "robot")

    def test_watchdog_and_leader_lease_latch_hold(self):
        self.activate()
        self.clock.step(0.251)
        self.assertIsNone(self.c.tick(self.q, True))
        self.assertEqual(self.c.reason, "COMMAND_TIMEOUT")
        self.c.request("reset", self.q, True)
        self.c.heartbeat(True)
        self.activate()
        self.clock.step(0.501)
        self.assertIsNone(self.c.tick(self.q, True))
        self.assertEqual(self.c.reason, "LEADER_LEASE_EXPIRED")

    def test_camera_health_and_recording_fault_stop(self):
        self.activate()
        self.assertIsNone(self.c.tick(self.q, False))
        self.assertEqual(self.c.mode, Mode.HOLD)
        self.c.request("reset", self.q, True)
        self.activate()
        self.c.recording = False
        self.assertIsNone(self.c.tick(self.q, True))
        self.assertEqual(self.c.reason, "RECORDING_REQUIRED")

    def test_alignment_and_prepare_timeout(self):
        self.c.request("arm", self.q, True)
        self.c.request("start", self.q, True)
        with self.assertRaisesRegex(Rejected, "ALIGNMENT_REQUIRED"):
            self.c.mode_ready(self.c.epoch, [1] * 14)
        self.clock.step(5.1)
        self.c.heartbeat(True)
        self.c.tick(self.q, True)
        self.assertEqual(self.c.reason, "TRANSITION_TIMEOUT")

    def test_velocity_and_acceleration_bounds_on_reversal(self):
        self.activate()
        previous = self.q.copy()
        velocity = [0.0] * 14
        for i in range(150):
            self.clock.step()
            self.c.heartbeat(True)
            packet = self.packet()
            packet["action"][0] = 0.2 if i < 70 else -0.2
            self.c.offer(packet, "robot")
            output = self.c.tick(previous, True).action
            next_velocity = [(a - b) / 0.01 for a, b in zip(output, previous)]
            for j in range(14):
                self.assertLessEqual(abs(next_velocity[j]), self.cfg.safety.velocity_max[j % 7] + 1e-8)
                self.assertLessEqual(
                    abs(next_velocity[j] - velocity[j]), self.cfg.safety.acceleration_max[j % 7] * 0.01 + 1e-8
                )
            previous, velocity = output, next_velocity

    def test_plain_teleop_uses_same_fields_without_intervention_label(self):
        self.cfg.general.operation_mode = "teleop"
        self.c.recording = False
        self.c.request("arm", self.q, True)
        self.c.request("start", self.q, True)
        self.c.mode_ready(self.c.epoch, self.q)
        self.offer(True)
        result = self.c.tick(self.q, True)
        self.assertEqual((result.hil_state, result.intervention, result.source), (0, 0, "human"))


class ConfigurationTests(unittest.TestCase):
    def test_hardware_can_monitor_but_cannot_arm_before_commissioning(self):
        cfg = AppConfig()
        cfg.general.simulation = False
        cfg.robot.backend, cfg.robot.stop_strategy = "ros", "position_hold"
        cfg.leader.backend = "ros"
        for arm in cfg.leader.arms:
            arm.mode_confirmation = "openeai_graph"
        for camera in cfg.cameras:
            camera.backend, camera.device = "usb", "/dev/video0"
        cfg.policy.backend = "openpi"
        cfg.network.gateway_url = "wss://localhost:8800"
        cfg = AppConfig.model_validate(cfg.model_dump())
        controller = Controller(cfg)
        controller.heartbeat(True)
        with self.assertRaisesRegex(Rejected, "HARDWARE_COMMISSIONING_REQUIRED"):
            controller.request("arm", [0.0] * 14, True)
        self.assertEqual(controller.mode, Mode.DISARMED)

    def test_fail_closed_hardware_and_invalid_configuration(self):
        for change in [
            dict(general={"simulation": False}),
            dict(network={"gateway_url": "ws://example.com"}),
            dict(safety={"joint_min": [0]}),
            dict(ui={"intervention_key": "s"}),
            dict(policy={"url": "wss://secret:password@example.com"}),
        ]:
            with self.assertRaises(ValueError):
                AppConfig.model_validate(change)

    def test_atomic_revision_conflict_and_deferred_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            atomic_json(path, AppConfig().model_dump())
            store = ConfigStore(path)
            original = store.read()
            edited = original["config"]
            edited["ui"]["language"] = "en"
            saved = store.save(edited, original["revision"])
            self.assertTrue(saved["restart_required"])
            self.assertEqual(store.current.ui.language, "zh")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "CONFIG_CONFLICT"):
                store.save(edited, original["revision"])

    def test_credentials_scoped_revocable_and_permission_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = AppConfig()
            cfg.security.credentials_file = str(Path(temp) / "credentials.json")
            create_credentials(cfg.security.credentials_file, cfg.general.robot_id)
            credentials = Credentials(cfg)
            token = credentials.token("client")
            self.assertTrue(credentials.verify(token, "client", cfg.general.robot_id))
            self.assertFalse(credentials.verify(token, "robot", cfg.general.robot_id))
            self.assertFalse(credentials.verify(token, "client", "another-robot"))
            Path(cfg.security.credentials_file).chmod(0o644)
            self.assertFalse(credentials.verify(token, "client", cfg.general.robot_id))
            atomic_json(Path(cfg.security.credentials_file), {"credentials": []})
            self.assertFalse(credentials.verify(token, "client", cfg.general.robot_id))

    def test_dataset_contract_and_path_confinement(self):
        cfg = AppConfig()
        features = hil_features(cfg.joint_names, {c.name: (480, 640, 3) for c in cfg.cameras})
        self.assertEqual(len(features), 9)
        self.assertEqual(features["action"]["names"][6], "left_gripper.pos")
        self.assertEqual(features["observation.images.main"]["shape"], (480, 640, 3))
        with tempfile.TemporaryDirectory() as temp:
            cfg.recording.root = temp
            inspector = DatasetInspector(cfg)
            for identifier in ["-1:dataset", "0:../outside", "0:..", "0:/tmp", "1:outside"]:
                with self.assertRaises(ValueError):
                    inspector._path(identifier)
            (Path(temp) / "escape").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                inspector._path("0:escape")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_priority_latest_only_and_bounded_transactions(self):
        outbox = Outbox()
        for seq in range(1000):
            outbox.put({"type": "human_action", "seq": seq})
        self.assertEqual(len(outbox.latest), 1)
        self.assertEqual((await outbox.get())["seq"], 999)
        for _ in range(64):
            outbox.put({"type": "rpc_request"})
        with self.assertRaises(asyncio.QueueFull):
            outbox.put({"type": "rpc_request"})
        outbox.put({"type": "stop"})
        self.assertEqual((await outbox.get())["type"], "stop")

    async def test_strict_wire_envelope(self):
        self.assertEqual(decode(encode({"type": "stop"}), 100)["type"], "stop")
        for raw in ["[]", '{"v":1,"type":"x","n":NaN}', '{"v":2,"type":"x"}', "x" * 101]:
            with self.assertRaises(ValueError):
                decode(raw, 100)


if __name__ == "__main__":
    unittest.main()
