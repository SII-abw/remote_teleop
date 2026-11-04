"""Subscribe to the live site graph and verify passive commissioning. Publishes no control."""

import json
import time
from collections import Counter
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig.model_validate_json((root / "configs/siinuc/slave.json").read_text())
    if cfg.general.simulation or cfg.safety.hardware_validated:
        raise SystemExit("Requires a real, motion-locked commissioning configuration")
    import os

    os.environ["ROS_DOMAIN_ID"] = str(cfg.robot.ros_domain_id)
    import rclpy
    from sensor_msgs.msg import JointState

    rclpy.init()
    node = rclpy.create_node("remote_teleop_passive_check", enable_rosout=False)
    commands = [a.command_topic for a in cfg.robot.arms] + [a.mode_topic for a in cfg.leader.arms]
    leader_targets = [a.target_topic for a in cfg.leader.arms]
    states = [a.state_topic for a in cfg.robot.arms] + [a.state_topic for a in cfg.leader.arms]
    counts = Counter()
    for topic in commands + leader_targets + states:

        def bind(target):
            def receive(msg):
                counts[target] += 1

            return receive

        node.create_subscription(JointState, topic, bind(topic), 10)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
        assert all(counts[topic] == 0 for topic in commands), dict(counts)
        assert all(counts[topic] > 100 for topic in states), dict(counts)
        report = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_seconds": 15,
            "ros_domain_id": cfg.robot.ros_domain_id,
            "received": dict(counts),
            "robot_target_and_mode_messages": {topic: counts[topic] for topic in commands},
            "leader_target_publishers": {
                topic: [endpoint.node_name for endpoint in node.get_publishers_info_by_topic(topic)]
                for topic in leader_targets
            },
            "note": "OpenEAI publishes joint_targets echoes in drag mode. Humble rclpy callbacks cannot attribute individual messages to a publisher.",
            "passive": True,
        }
        atomic_json(root / "state/siinuc/passive-ros-check.json", report)
        print(json.dumps(report, indent=2))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
