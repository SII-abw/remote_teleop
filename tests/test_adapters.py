"""Driver contract tests with in-memory buses; no ROS or serial libraries are loaded."""

import threading
import time
import unittest
from types import SimpleNamespace

from remote_teleop.adapters.hardware import PositionHoldRobot, RosLeader, RosRobot, SerialLeader
from remote_teleop.config import AppConfig
from remote_teleop.control import Rejected


class Bus:
    def __init__(self, events, torque):
        self.events, self.torque = events, torque

    def sync_read(self, register, **_):
        self.events.append(("read", register))
        return {str(i): self.torque if register == "Torque_Enable" else 1234 for i in range(7)}

    def sync_write(self, register, values, **_):
        self.events.append(("write", register, values))


class AdapterContractTests(unittest.TestCase):
    def test_position_hold_boot_is_passive_and_stop_captures_once(self):
        robot = PositionHoldRobot.__new__(PositionHoldRobot)
        robot.cfg = AppConfig()
        robot.lock = threading.RLock()
        robot.stopped, robot.last_target, robot.hold_target = True, None, None
        measured = [0.1] * 14
        robot.read = lambda: (measured.copy(), time.monotonic())
        robot.io = SimpleNamespace(stationary=lambda *args: True)
        robot.targets = {
            side: SimpleNamespace(get_subscription_count=lambda: 1) for side in ("left", "right")
        }
        commands = []
        robot._publish = lambda q: commands.append(q.copy())
        robot.stop()
        self.assertEqual(commands, [])
        self.assertTrue(robot.stop_confirmed())
        robot.enable()
        robot.command([0.2] * 14)
        robot.stop()
        expected = measured.copy()
        expected[6] = expected[13] = 0.2
        self.assertEqual(commands[-1], expected)
        measured[0] = 0.3
        robot.stop()
        self.assertEqual(commands[-1][0], 0.1)  # never chase motion while stopped
        with self.assertRaisesRegex(Rejected, "DRIVER_NOT_ENABLED"):
            robot.command([0] * 14)
        robot.read = lambda: ([0.0] * 14, time.monotonic() - 5)
        self.assertFalse(robot.stop_confirmed())
        with self.assertRaisesRegex(Rejected, "STALE_JOINT_STATE"):
            robot.enable()

    def test_openeai_mode_uses_live_driver_graph_not_static_parameter(self):
        leader = RosLeader.__new__(RosLeader)
        leader.cfg = AppConfig()
        arm = leader.cfg.leader.arms[0]
        arm.mode_confirmation = "openeai_graph"
        driver = SimpleNamespace(node_name=arm.driver_node)
        echo, subscribers = [driver], [driver]
        leader.io = SimpleNamespace(
            lock=threading.RLock(),
            states={"left": ([], time.monotonic())},
            node=SimpleNamespace(
                get_publishers_info_by_topic=lambda _: echo,
                get_subscriptions_info_by_topic=lambda _: subscribers,
            ),
        )
        started = time.monotonic() - 0.3
        self.assertTrue(leader._mode_confirmed(arm, True, started))
        self.assertFalse(leader._mode_confirmed(arm, False, started))
        echo.clear()
        self.assertTrue(leader._mode_confirmed(arm, False, started))
        subscribers.clear()
        self.assertFalse(leader._mode_confirmed(arm, False, started))

    def serial(self, torque):
        adapter = SerialLeader.__new__(SerialLeader)
        adapter.cfg = AppConfig()
        adapter.lock = threading.RLock()
        events = []
        bus = Bus(events, torque)
        adapter.device = SimpleNamespace(
            _arms={"left": SimpleNamespace(bus=bus), "right": SimpleNamespace(bus=bus)},
            set_manual_control=lambda manual: events.append(("mode", manual)),
        )
        adapter.manual = True
        return adapter, events

    def test_serial_preloads_both_goals_before_enabling_torque(self):
        adapter, events = self.serial(1)
        adapter.set_manual(False)
        mode_index = events.index(("mode", False))
        self.assertEqual(sum(e[:2] == ("write", "Goal_Position") for e in events[:mode_index]), 2)
        self.assertFalse(adapter.manual)
        self.assertEqual(sum(e[:2] == ("read", "Torque_Enable") for e in events[mode_index:]), 2)

    def test_serial_rejects_unconfirmed_torque_mode(self):
        adapter, _ = self.serial(0)
        with self.assertRaisesRegex(Rejected, "LEADER_MODE_TIMEOUT"):
            adapter.set_manual(False)
        self.assertTrue(adapter.manual)

    def test_robot_stop_does_not_publish_joint_targets_and_requires_fresh_ack(self):
        robot = RosRobot.__new__(RosRobot)
        robot.cfg = AppConfig()
        robot.stopped, robot.last_stop_request = False, 0
        messages = []
        robot.stops = {
            side: SimpleNamespace(publish=lambda m: messages.append(m.data)) for side in ("left", "right")
        }
        robot.io = SimpleNamespace(Bool=lambda: SimpleNamespace(data=False), lock=threading.Lock(), modes={})
        robot.stop()
        self.assertEqual(messages, [True, True])
        self.assertFalse(robot.stop_confirmed())
        now = time.monotonic()
        robot.io.modes = {s: (True, now) for s in ("left", "right")}
        self.assertTrue(robot.stop_confirmed())
        robot.io.modes["right"] = (True, now - 1)
        self.assertFalse(robot.stop_confirmed())
        with self.assertRaisesRegex(Rejected, "DRIVER_NOT_ENABLED"):
            robot.command([0] * 14)


if __name__ == "__main__":
    unittest.main()
