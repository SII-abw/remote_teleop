"""Offline bounds for the explicitly attended gripper probe."""

import importlib.util
from pathlib import Path
import unittest

from remote_teleop.config import AppConfig
from remote_teleop.control import Controller, Mode, Rejected

spec = importlib.util.spec_from_file_location("probe", Path(__file__).parents[1] / "scripts/probe_gripper.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_only_gripper_target_changes_and_step_is_bounded(self):
        baseline = [0.1] * 14
        for side in ("left", "right"):
            targets = [probe.target_at(baseline, side, step) for step in range(41)]
            self.assertTrue(all(target[:6] == [0.1] * 6 for target in targets))
            self.assertEqual(targets[0][6], 0)
            self.assertEqual(targets[-1][6], 0.002)
        with self.assertRaises(ValueError):
            probe.target_at(baseline, "left", 41)

    def test_joint_motion_other_gripper_motion_and_invalid_feedback_abort(self):
        baseline = [0.0] * 14
        probe.check_feedback(baseline, baseline, "left")
        for index, value in ((0, 0.021), (7, 0.021), (13, 0.021), (6, 0.251), (6, -0.031), (6, float("nan"))):
            current = baseline.copy()
            current[index] = value
            with self.assertRaises(ValueError):
                probe.check_feedback(baseline, current, "left")


class AttendedTests(unittest.TestCase):
    def setUp(self):
        cfg = AppConfig()
        cfg.general.operation_mode = "teleop"
        cfg.robot.stop_strategy = "position_hold"
        cfg.safety.attended_test.enabled = True
        cfg.safety.attended_test.expires_at = 1100
        cfg.safety.attended_test.gripper_commands = [0.002, 0]
        self.cfg = AppConfig.model_validate(cfg.model_dump())
        self.now, self.wall = 100.0, 1000.0
        self.c = Controller(self.cfg, lambda: self.now, lambda: self.wall)
        self.q = [0.0] * 14
        self.q[6], self.q[13] = 0.02174, -0.00191

    def activate(self):
        self.c.heartbeat(True)
        self.c.request("arm", self.q, True)
        self.c.request("start", self.q, True)
        self.c.mode_ready(self.c.epoch, self.c.last_target)

    def offer(self, delta=0.04):
        action = [delta] * 14
        self.c.offer(
            dict(
                type="human_action",
                epoch=self.c.epoch,
                seq=self.c.last_seq + 1,
                observation_id=self.c.observe(),
                action=action,
            ),
            "client",
        )

    def test_raw_gripper_feedback_stays_intact_and_commands_stay_fixed(self):
        self.activate()
        for _ in range(100):
            self.now += 0.01
            self.c.heartbeat(True)
            self.offer()
            result = self.c.tick(self.q, True)
            self.assertIsNotNone(result)
            self.assertEqual([result.action[i] for i in (6, 13)], [0.002, 0])
            self.assertLessEqual(max(abs(v) for v in self.c.velocity), 0.05)
            self.assertLessEqual(result.action[0], 0.05)
        self.assertEqual(self.q[6:7], [0.02174])
        self.assertEqual(self.q[13], -0.00191)

    def test_disabled_joint_range_allows_motion_beyond_three_degrees(self):
        self.cfg.safety.attended_test.joint_delta = None
        self.activate()
        for _ in range(300):
            self.now += 0.01
            self.c.heartbeat(True)
            self.offer(0.2)
            result = self.c.tick(self.q, True)
            self.assertIsNotNone(result)
            self.assertEqual([result.action[i] for i in (6, 13)], [0.002, 0])
            self.assertLessEqual(max(abs(v) for v in self.c.velocity), 0.05)
            for i in range(14):
                if i % 7 != 6:
                    self.q[i] = result.action[i]
        self.assertGreater(self.q[0], 0.05)
        self.assertGreater(self.q[7], 0.05)
        self.assertEqual(self.c.mode, Mode.HUMAN)

    def test_disabled_joint_range_preserves_position_and_gripper_limits(self):
        self.cfg.safety.attended_test.joint_delta = None
        self.activate()
        with self.assertRaisesRegex(Rejected, "POSITION_LIMIT"):
            self.offer(3.15)
        self.q[6] += 0.021
        self.assertIsNone(self.c.tick(self.q, True))
        self.assertEqual(self.c.reason, "ATTENDED_TEST_FEEDBACK_RANGE")

    def test_range_violation_holds_and_cannot_rearm(self):
        self.activate()
        with self.assertRaisesRegex(Rejected, "ATTENDED_TEST_RANGE"):
            self.offer(0.051)
        self.assertEqual(self.c.mode, Mode.HOLD)
        self.c.request("reset", self.q, True)
        with self.assertRaisesRegex(Rejected, "ATTENDED_TEST_UNAVAILABLE"):
            self.c.request("arm", self.q, True)

    def test_expiry_and_duration_stop_even_with_fresh_commands(self):
        for wall, elapsed in ((1101, 0), (1000, 121)):
            self.setUp()
            self.activate()
            self.wall, self.now = wall, 100 + elapsed
            self.c.heartbeat(True)
            self.assertIsNone(self.c.tick(self.q, True))
            self.assertEqual(self.c.reason, "ATTENDED_TEST_EXPIRED")

    def test_unexpected_gripper_or_joint_motion_stops(self):
        for index, delta in ((0, 0.056), (6, 0.021)):
            self.setUp()
            self.activate()
            self.q[index] += delta
            self.assertIsNone(self.c.tick(self.q, True))
            self.assertEqual(self.c.reason, "ATTENDED_TEST_FEEDBACK_RANGE")

    def test_test_cannot_enable_hil_or_claim_production_validation(self):
        for path, value in (("mode", "hil"), ("validated", True), ("commands", [0.086, 0])):
            raw = self.cfg.model_dump()
            if path == "mode":
                raw["general"]["operation_mode"] = value
            elif path == "validated":
                raw["safety"]["hardware_validated"] = value
            else:
                raw["safety"]["attended_test"]["gripper_commands"] = value
            with self.assertRaises(ValueError):
                AppConfig.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
