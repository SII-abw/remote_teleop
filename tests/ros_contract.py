"""ROS transport regression in domain 218 with fake drivers. Never uses domain 0.

Run after sourcing ROS Humble, with evo-rl Python and PYTHONPATH=src.
Tests actual DDS QoS, passive boot, measured hold, and OpenEAI mode discovery.
"""

import os
import threading
import time

os.environ["ROS_DOMAIN_ID"] = "218"

import rclpy
from rclpy.executors import MultiThreadedExecutor
from sensor_msgs.msg import JointState
from std_msgs.msg import Int32

from remote_teleop.adapters.hardware import PositionHoldRobot, RosLeader
from remote_teleop.config import AppConfig
from remote_teleop.homing import HomingSession


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except ValueError:
            pass
        time.sleep(0.02)
    raise AssertionError("ROS fixture condition timed out")


def main():
    rclpy.init()
    fixture = rclpy.create_node("remote_teleop_test_fixture")
    time.sleep(0.3)
    if any(name != fixture.get_name() for name, _ in fixture.get_node_names_and_namespaces()):
        raise SystemExit("Domain 218 is in use; refusing to publish fixture data")
    cfg = AppConfig()
    cfg.robot.ros_domain_id = 218
    cfg.leader.ros_domain_id = 218
    cfg.robot.stop_strategy = "position_hold"
    for arm in cfg.leader.arms:
        arm.mode_confirmation = "openeai_graph"
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(fixture)
    nodes, targets, echoes, positions, received, active = {}, {}, {}, {}, {}, {"states": True}
    resets = {side: [] for side in ("left", "right")}
    for arm in cfg.robot.arms:
        side = arm.name
        leader_cfg = next(a for a in cfg.leader.arms if a.name == side)
        node = rclpy.create_node("OpenEAIArm_node")
        nodes[side] = node
        executor.add_node(node)
        positions[side], received[side] = [0.0] * 7, []
        targets[side] = fixture.create_publisher(JointState, arm.state_topic, 10)
        leader_state = node.create_publisher(JointState, leader_cfg.state_topic, 10)
        echoes[side] = node.create_publisher(JointState, leader_cfg.target_topic, 10)

        def command(msg, side=side):
            received[side].append(list(msg.position))
            positions[side] = list(msg.position)

        def mode(msg, side=side, node=node, topic=leader_cfg.target_topic):
            if msg.position[0] == 0 and echoes[side] is not None:
                node.destroy_publisher(echoes[side])
                echoes[side] = None
            elif msg.position[0] == 1 and echoes[side] is None:
                echoes[side] = node.create_publisher(JointState, topic, 10)

        fixture.create_subscription(JointState, arm.command_topic, command, 10)
        node.create_subscription(JointState, leader_cfg.target_topic, lambda _: None, 10)
        node.create_subscription(JointState, leader_cfg.mode_topic, mode, 10)

        def reset(message, side=side):
            resets[side].append(message.data)
            if message.data == 1:
                positions[side] = [0.0] * 7

        node.create_subscription(Int32, "/fixture/robot_reset", reset, 10)

        def tick(side=side, names=arm.joint_names, leader_state=leader_state):
            if not active["states"]:
                return
            msg = JointState()
            msg.name, msg.position = names, positions[side]
            targets[side].publish(msg)
            leader_state.publish(msg)
            if echoes[side] is not None:
                echoes[side].publish(msg)

        fixture.create_timer(0.01, tick)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    robot, leader = PositionHoldRobot(cfg), RosLeader(cfg)
    try:
        until(robot.stop_confirmed)
        robot.stop()
        assert all(not values for values in received.values()), "Boot published a robot target"
        robot.enable()
        until(robot.enabled_confirmed)
        robot.command([0.01] * 14)
        until(lambda: all(values and values[-1] == [0.01] * 7 for values in received.values()))
        robot.stop()
        until(robot.stop_confirmed)
        leader.set_manual(False)
        assert not leader.manual and all(echo is None for echo in echoes.values())
        leader.feedback([0.01] * 14)
        leader.set_manual(True)
        assert leader.manual and all(echo is not None for echo in echoes.values())
        cfg.leader.backend = "ros"
        cfg.leader.homing.reset_topic = "/fixture/robot_reset"
        home = HomingSession(cfg, leader, "leader")
        try:

            def home_tick():
                home.tick()
                return home.state == "COMPLETE"

            until(home_tick, timeout=8)
            until(
                lambda: all(1 in values and 0 in values[values.index(1) + 1 :] for values in resets.values())
            )
            assert all(len(values) >= 3 for values in resets.values()), resets
        finally:
            home.close()
        active["states"] = False
        time.sleep(0.2)
        assert not robot.stop_confirmed()
        print(
            "PASS: domain 218 DDS reliable commands, passive boot, position hold, mode round trip, two-subscriber native reset/exit, stale feedback"
        )
    finally:
        robot.close()
        leader.close()
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        for node in [fixture, *nodes.values()]:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
