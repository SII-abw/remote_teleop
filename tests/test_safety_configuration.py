"""Configurable guards and unrestricted manual sessions, without hardware I/O."""

import math
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote_teleop.adapters.hardware import RosRobot, RosIO
from remote_teleop.config import AppConfig, ConfigStore
from remote_teleop.control import Controller, Mode, Rejected


def manual_config():
    cfg = AppConfig()
    cfg.general.operation_mode = "teleop"
    cfg.robot.stop_strategy = "position_hold"
    trial = cfg.safety.attended_test
    trial.enabled = True
    for key in (
        "expires_at",
        "duration_seconds",
        "max_start_window_seconds",
        "joint_delta",
        "gripper_commands",
        "gripper_baseline_limit",
        "gripper_feedback_delta",
        "velocity_cap",
        "acceleration_cap",
    ):
        setattr(trial, key, None)
    trial.single_use = False
    trial.allow_recording = True
    return AppConfig.model_validate(cfg.model_dump())


class ConfigurableSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = manual_config()
        self.now, self.wall = 100.0, 1000.0
        self.c = Controller(self.cfg, lambda: self.now, lambda: self.wall)
        self.q = [0.0] * 14

    def activate(self):
        self.c.heartbeat(True)
        self.c.request("arm", self.q, True)
        self.c.request("start", self.q, True)
        self.c.mode_ready(self.c.epoch, self.c.last_target)

    def offer(self, target, observation=None):
        self.c.offer(
            {
                "type": "human_action",
                "epoch": self.c.epoch,
                "seq": self.c.last_seq + 1,
                "observation_id": observation or self.c.observe(),
                "action": target,
            },
            "client",
        )

    def test_roundtrip_unlimited_and_all_guards_enter_contract(self):
        self.assertEqual(AppConfig.model_validate_json(self.cfg.model_dump_json()), self.cfg)
        for field in type(self.cfg.safety.checks).model_fields:
            changed = self.cfg.model_copy(deep=True)
            setattr(changed.safety.checks, field, False)
            self.assertNotEqual(self.cfg.control_contract, changed.control_contract, field)

    def test_config_form_receives_defaults_for_older_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}")
            store = ConfigStore(path)
            result = store.read()
            self.assertTrue(result["config"]["safety"]["checks"]["hardware_validation"])
            self.assertEqual(result["config"]["safety"]["attended_test"]["duration_seconds"], 120)
            self.assertFalse(result["restart_required"])
            self.assertEqual(path.read_text(), "{}")

    def test_complete_manual_motion_and_grippers_without_test_caps(self):
        self.activate()
        target = [0.4] * 14
        target[6], target[13] = 0.04, 0.03
        peak_velocity = 0
        for _ in range(300):
            self.now += 0.01
            self.c.heartbeat(True)
            self.offer(target)
            result = self.c.tick(self.q, True)
            self.assertIsNotNone(result, self.c.reason)
            peak_velocity = max(peak_velocity, abs(self.c.velocity[0]))
            self.q = result.action
        self.assertGreater(self.q[0], 0.35)
        self.assertGreater(self.q[6], 0.03)
        self.assertGreater(self.q[13], 0.02)
        self.assertGreater(peak_velocity, 0.05)
        self.assertLessEqual(peak_velocity, self.cfg.safety.velocity_max[0])
        self.assertEqual(self.c.mode, Mode.HUMAN)

    def test_unlimited_duration_and_repeat_arm_after_pause(self):
        self.activate()
        self.now += 10000
        self.wall += 100000
        self.c.heartbeat(True)
        self.offer(self.q)
        self.assertIsNotNone(self.c.tick(self.q, True))
        self.c.request("pause", self.q, True)
        self.c.request("reset", self.q, True)
        self.activate()
        self.assertEqual(self.c.mode, Mode.HUMAN)

    def test_gripper_endpoints_do_not_trigger_position_hold(self):
        for endpoint in (0.085, 0.0):
            self.setUp()
            self.q[6] = self.q[13] = 0.085 - endpoint
            self.activate()
            target = self.q.copy()
            target[6] = target[13] = endpoint
            previous_velocity = 0.0
            for _ in range(500):
                self.now += 0.01
                self.c.heartbeat(True)
                self.offer(target)
                result = self.c.tick(self.q, True)
                self.assertIsNotNone(result, self.c.reason)
                self.assertLessEqual(
                    abs(self.c.velocity[6] - previous_velocity),
                    self.cfg.safety.acceleration_max[6] * 0.01 + 1e-10,
                )
                previous_velocity = self.c.velocity[6]
                self.q = result.action
            self.assertAlmostEqual(self.q[6], endpoint)

    def test_normal_position_limit_and_manual_stop_remain(self):
        self.activate()
        target = self.q.copy()
        target[0] = 4
        with self.assertRaisesRegex(Rejected, "POSITION_LIMIT"):
            self.offer(target)
        self.c.request("estop", self.q, True)
        with self.assertRaisesRegex(Rejected, "RESET_REQUIRED"):
            self.c.request("disarm", self.q, True)
        self.assertIsNone(self.c.tick(self.q, True))

    def test_gripper_boundary_with_variable_control_timing(self):
        self.cfg.safety.velocity_max[6] = 0.03
        self.cfg.safety.acceleration_max[6] = 0.1
        self.q[6], self.q[13] = 0.00015, 0.00004
        self.activate()
        target = [0.0] * 14
        intervals = (0.0101, 0.0117, 0.0098, 0.02, 0.0001)
        for step in range(1000):
            dt = intervals[step % len(intervals)]
            self.now += dt
            self.c.heartbeat(True)
            self.offer(target)
            result = self.c.tick(self.q, True)
            self.assertIsNotNone(result, self.c.limit_detail)
            self.q = result.action
        self.assertAlmostEqual(self.q[6], 0)
        self.assertAlmostEqual(self.q[13], 0)

    def test_rate_guards_can_be_disabled_without_nan_or_overshoot(self):
        self.cfg.safety.checks.velocity_limits = False
        self.cfg.safety.checks.acceleration_limits = False
        self.activate()
        target = self.q.copy()
        target[0] = 0.8
        self.offer(target)
        self.now += 0.01
        result = self.c.tick(self.q, True)
        self.assertEqual(result.action, target)
        self.q = result.action
        self.now += 0.01
        self.assertTrue(all(math.isfinite(v) for v in self.c.tick(self.q, True).action))

    def test_guard_switches_and_protocol_validation_are_independent(self):
        for field in (
            "position_limits",
            "tracking_error",
            "command_timeout",
            "command_freshness",
            "leader_heartbeat",
            "alignment",
        ):
            setattr(self.cfg.safety.checks, field, False)
        self.activate()
        observation = self.c.observe()
        self.now += 2
        target = [4.0] * 14
        self.offer(target, observation)
        self.q = [1.0] * 14
        self.assertIsNotNone(self.c.tick(self.q, True))
        with self.assertRaisesRegex(Rejected, "INVALID_NUMBER"):
            self.offer([float("nan")] * 14)
        self.assertEqual(self.c.mode, Mode.HUMAN)

    def test_trial_caps_are_separate_from_normal_rate_switches(self):
        self.cfg.safety.checks.velocity_limits = False
        self.cfg.safety.checks.acceleration_limits = False
        self.cfg.safety.attended_test.velocity_cap = 0.02
        self.activate()
        self.offer([0.04] * 14)
        self.now += 0.01
        self.assertIsNotNone(self.c.tick(self.q, True))
        self.assertLessEqual(max(self.c.velocity), 0.02)

    def test_gripper_feedback_converts_only_gripper_coordinates(self):
        robot = RosRobot.__new__(RosRobot)
        robot.cfg = self.cfg
        for arm in self.cfg.robot.arms:
            arm.gripper_feedback_scale = 0.02
            arm.gripper_feedback_offset = 0.001
        raw = [0.4] * 14
        robot.io = SimpleNamespace(read=lambda: (raw.copy(), 123))
        q, stamp = robot.read()
        self.assertEqual(stamp, 123)
        self.assertAlmostEqual(q[6], 0.009)
        self.assertAlmostEqual(q[13], 0.009)
        self.assertEqual(q[0], 0.4)
        self.assertEqual(raw[6], 0.4)

    def test_stationary_confirmation_uses_configured_thresholds(self):
        io = RosIO.__new__(RosIO)
        io.lock = threading.RLock()
        now = time.monotonic()
        start, end = [0.0] * 7, [0.0] * 7
        end[6] = 0.003
        io.history = {n: [(start, now - 0.24), (start, now - 0.12), (end, now)] for n in ("left", "right")}
        self.assertFalse(io.stationary())
        self.assertTrue(io.stationary(gripper_tolerance=0.004))
        self.assertFalse(io.stationary(gripper_tolerance=0.004, min_samples=4))


if __name__ == "__main__":
    unittest.main()
