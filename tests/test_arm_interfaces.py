"""Configurable ROS contracts using an in-memory ROS bus, without arm motion."""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from remote_teleop.adapters import hardware
from remote_teleop.config import AppConfig
from remote_teleop.control import Rejected


class JointState:
    def __init__(self):
        self.header = SimpleNamespace(stamp=None)
        self.name, self.position = [], []


class Bool:
    def __init__(self):
        self.data = False


class Int32:
    def __init__(self):
        self.data = 0


class Publisher:
    def __init__(self, node, message_type, topic):
        self.node, self.message_type, self.topic = node, message_type, topic
        self.messages = []

    def publish(self, msg):
        self.messages.append(msg)
        self.node.events.append((self.topic, msg))
        callback = self.node.on_publish.get(self.topic)
        if callback:
            callback(msg)

    def get_subscription_count(self):
        return 1


class Node:
    def __init__(self):
        self.publishers, self.subscriptions, self.events, self.on_publish = {}, {}, [], {}

    def create_publisher(self, message_type, topic, qos):
        publisher = Publisher(self, message_type, topic)
        self.publishers[topic] = publisher
        return publisher

    def create_subscription(self, message_type, topic, callback, qos):
        self.subscriptions[topic] = (message_type, callback)

    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "now"))


class RosBus:
    JointState, Bool, Int32 = JointState, Bool, Int32
    mode_type = hardware.RosIO.mode_type
    mode_message = hardware.RosIO.mode_message
    subscribe_mode = hardware.RosIO.subscribe_mode
    subscribe_state = hardware.RosIO.subscribe_state
    read = hardware.RosIO.read

    def __init__(self, domain, name, history_seconds=0.3):
        self.domain, self.name, self.history_seconds = domain, name, history_seconds
        self.node, self.lock, self.qos, self.command_qos = Node(), threading.RLock(), 1, 1
        self.states, self.modes = {}, {}
        self.history = {side: [] for side in ("left", "right")}

    def state(self, topic, names, values):
        msg = JointState()
        msg.name, msg.position = names, values
        self.node.subscriptions[topic][1](msg)

    def acknowledge_on_publish(self, arm):
        def receive(msg):
            value = msg.position[0] if arm.mode_message_type == "joint_state" else msg.data
            state = hardware.RosIO.mode_type(self, arm.mode_message_type)()
            if arm.mode_message_type == "joint_state":
                state.position = [value]
            else:
                state.data = value
            self.node.subscriptions[arm.mode_state_topic][1](state)

        self.node.on_publish[arm.mode_topic] = receive

    def stationary(self, *args):
        return True

    def close(self):
        pass


class ArmInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.safety.transition_timeout_seconds = 0.06
        for arm in self.cfg.leader.arms:
            arm.mode_state_topic = f"/leader/{arm.name}/mode_state"

    def leader_states(self, adapter, values=None):
        for arm in self.cfg.leader.arms:
            adapter.io.state(arm.state_topic, arm.joint_names, values or [0.0] * 7)

    def robot_states(self, adapter):
        for arm in self.cfg.robot.arms:
            adapter.io.state(arm.state_topic, arm.joint_names, [0.03] * 7)

    @patch.object(hardware, "RosIO", RosBus)
    def test_position_hold_uses_last_command_if_stop_feedback_is_out_of_range(self):
        robot = hardware.PositionHoldRobot(self.cfg)
        self.robot_states(robot)
        robot.enable()
        robot.command([0.02] * 14)
        arm = self.cfg.robot.arms[0]
        robot.io.state(arm.state_topic, arm.joint_names, [4.0] + [0.03] * 6)
        robot.stop()
        self.assertTrue(robot.stopped)
        self.assertEqual(robot.targets["left"].messages[-1].position, [0.02] * 7)

    @patch.object(hardware, "RosIO", RosBus)
    def test_passive_boot_and_independent_domains_and_joint_names(self):
        original_domain = os.environ.get("ROS_DOMAIN_ID")
        self.cfg.robot.ros_domain_id, self.cfg.leader.ros_domain_id = 218, 219
        self.cfg.robot.arms[0].joint_names = [f"robot{i}" for i in range(7)]
        self.cfg.leader.arms[0].joint_names = [f"leader{i}" for i in range(7)]
        robot, hold, leader = (
            hardware.RosRobot(self.cfg),
            hardware.PositionHoldRobot(self.cfg),
            hardware.RosLeader(self.cfg),
        )
        self.assertEqual((robot.io.domain, hold.io.domain, leader.io.domain), (218, 218, 219))
        self.assertEqual(os.environ.get("ROS_DOMAIN_ID"), original_domain)
        for adapter in (robot, hold, leader):
            self.assertEqual(adapter.io.node.events, [])
        self.leader_states(leader, list(range(7)))
        self.assertEqual(leader.read()[0][:7], list(range(7)))
        for arm in self.cfg.leader.arms:
            leader.io.acknowledge_on_publish(arm)
        leader.set_manual(False)
        self.assertEqual(leader.targets["left"].messages[-1].name, self.cfg.leader.arms[0].joint_names)
        self.assertNotEqual(leader.targets["left"].messages[-1].name, self.cfg.robot.arms[0].joint_names)

    @patch.object(hardware, "RosIO", RosBus)
    def test_leader_gripper_feedback_conversion_does_not_rescale_commands(self):
        for arm in self.cfg.leader.arms:
            arm.gripper_feedback_scale, arm.gripper_feedback_offset = 0.02, 0.001
            arm.directions = [-1.0] * 6 + [2.0]
            arm.offsets = [0.1] * 7
        leader = hardware.RosLeader(self.cfg)
        self.leader_states(leader, [0.2] * 6 + [0.7])
        current, _ = leader.read()
        self.assertEqual(current[:6], [-0.1] * 6)
        self.assertAlmostEqual(current[6], 0.13)
        for arm in self.cfg.leader.arms:
            leader.io.acknowledge_on_publish(arm)
        leader.set_manual(False)
        leader.feedback(current)
        target = leader.targets["left"].messages[-1].position
        self.assertEqual(target[:6], [0.2] * 6)
        self.assertAlmostEqual(target[6], 0.015)

    @patch.object(hardware, "RosIO", RosBus)
    def test_receive_maps_names_and_invalid_names_remove_state(self):
        robot = hardware.PositionHoldRobot(self.cfg)
        for arm in self.cfg.robot.arms:
            robot.io.state(arm.state_topic, list(reversed(arm.joint_names)), list(range(7)))
        self.assertEqual(robot.read()[0], list(reversed(range(7))) * 2)
        arm = self.cfg.robot.arms[0]
        robot.io.state(arm.state_topic, ["bad"] * 7, list(range(7)))
        with self.assertRaisesRegex(Rejected, "JOINT_STATE_UNAVAILABLE"):
            robot.read()

    @patch.object(hardware, "RosIO", RosBus)
    def test_optional_mode_supports_joint_read_and_targets_without_mode_commands(self):
        for arm in self.cfg.leader.arms:
            arm.mode_switch_enabled = False
            arm.mode_topic = arm.mode_state_topic = ""
        leader = hardware.RosLeader(self.cfg)
        self.leader_states(leader)
        leader.set_manual(True)
        self.assertTrue(leader.manual)
        self.assertEqual(leader.mode_pubs, {})
        self.assertEqual(leader.io.node.events, [])
        with self.assertRaisesRegex(Rejected, "LEADER_MANUAL"):
            leader.feedback([0] * 14)
        leader.set_manual(False)
        self.assertFalse(leader.manual)
        self.assertEqual(
            [topic for topic, _ in leader.io.node.events],
            [arm.target_topic for arm in self.cfg.leader.arms],
        )
        self.assertTrue(
            all(publisher.messages[-1].position == [0.0] * 7 for publisher in leader.targets.values())
        )
        leader.feedback([0.02] * 14)
        self.assertTrue(
            all(publisher.messages[-1].position == [0.02] * 7 for publisher in leader.targets.values())
        )
        leader.set_manual(True)
        self.assertTrue(leader.manual)
        self.assertEqual(leader.mode_pubs, {})
        self.assertEqual(
            {topic for topic, _ in leader.io.node.events},
            {arm.target_topic for arm in self.cfg.leader.arms},
        )
        with self.assertRaisesRegex(Rejected, "LEADER_MANUAL"):
            leader.feedback([0] * 14)

    @patch.object(hardware, "RosIO", RosBus)
    def test_optional_mode_still_requires_fresh_state_before_feedback_ownership(self):
        for arm in self.cfg.leader.arms:
            arm.mode_switch_enabled = False
        leader = hardware.RosLeader(self.cfg)
        self.leader_states(leader)
        for side, (q, stamp) in leader.io.states.items():
            leader.io.states[side] = (q, stamp - 1)
        with self.assertRaisesRegex(Rejected, "STALE_LEADER_STATE"):
            leader.set_manual(False)
        self.assertTrue(leader.manual)
        self.assertEqual(leader.io.node.events, [])

    @patch.object(hardware, "RosIO", RosBus)
    def test_configured_mode_types_values_and_preload_before_transition(self):
        for kind, position, mit in (("joint_state", 4, 7), ("bool", 1, 0), ("int32", -2, 9)):
            with self.subTest(kind=kind):
                for arm in self.cfg.leader.arms:
                    arm.mode_message_type, arm.mode_position_value, arm.mode_mit_value = kind, position, mit
                leader = hardware.RosLeader(self.cfg)
                self.leader_states(leader)
                for arm in self.cfg.leader.arms:
                    leader.io.acknowledge_on_publish(arm)
                leader.set_manual(False)
                self.assertFalse(leader.manual)
                self.assertEqual(
                    [topic for topic, _ in leader.io.node.events[:2]],
                    [arm.target_topic for arm in self.cfg.leader.arms],
                )
                for arm in self.cfg.leader.arms:
                    msg = leader.mode_pubs[arm.name].messages[-1]
                    self.assertEqual(
                        msg.position if kind == "joint_state" else msg.data,
                        [float(position), 0] if kind == "joint_state" else position,
                    )
                leader.set_manual(True)
                for arm in self.cfg.leader.arms:
                    msg = leader.mode_pubs[arm.name].messages[-1]
                    self.assertEqual(
                        msg.position if kind == "joint_state" else msg.data,
                        [float(mit), 0] if kind == "joint_state" else mit,
                    )

    @patch.object(hardware, "RosIO", RosBus)
    def test_missing_or_stale_mode_ack_cannot_enable_feedback(self):
        leader = hardware.RosLeader(self.cfg)
        self.leader_states(leader)
        with self.assertRaisesRegex(Rejected, "LEADER_MODE_TIMEOUT"):
            leader.set_manual(False)
        self.assertTrue(leader.manual)
        now = time.monotonic()
        arm = self.cfg.leader.arms[0]
        for stamp, value in ((now - 5, 0), (now, 1), (now - 0.001, 0)):
            leader.io.modes[arm.name] = (value, stamp)
            self.assertFalse(leader._mode_confirmed(arm, False, now))
        leader.io.modes[arm.name] = (0, now)
        self.assertTrue(leader._mode_confirmed(arm, False, now))
        leader.io.modes[arm.name] = (0, now - 1)
        self.assertFalse(leader._mode_confirmed(arm, False, now - 2))

    @patch.object(hardware, "RosIO", RosBus)
    def test_robot_optional_mode_switch_preloads_and_requires_confirmation(self):
        for arm in self.cfg.robot.arms:
            arm.mode_switch_enabled = True
            arm.mode_topic, arm.mode_state_topic = f"/{arm.name}/mode", f"/{arm.name}/mode_state"
            arm.mode_message_type = "int32"
            arm.mode_position_value = 5
        robot = hardware.PositionHoldRobot(self.cfg)
        self.robot_states(robot)
        with self.assertRaisesRegex(Rejected, "DRIVER_MODE_TIMEOUT"):
            robot.enable()
        self.assertTrue(robot.stopped)
        for arm in self.cfg.robot.arms:
            robot.io.acknowledge_on_publish(arm)
        robot.enable()
        self.assertTrue(robot.enabled_confirmed())
        for arm in self.cfg.robot.arms:
            self.assertEqual(robot.mode_pubs[arm.name].messages[-1].data, 5)
        self.assertEqual(robot.targets["left"].messages[0].position, [0.03] * 7)


if __name__ == "__main__":
    unittest.main()
