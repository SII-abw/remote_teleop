"""ROS driver contracts and the built-in Feetech leader adapter."""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from ..control import Rejected, vector


class SimRobot:
    def __init__(self, cfg):
        self.q = [0.0] * 14
        self.stopped = True
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            return self.q.copy(), time.monotonic()

    def command(self, target):
        with self.lock:
            if self.stopped:
                raise Rejected("DRIVER_STOPPED")
            self.q = vector(target)

    def stop(self):
        self.stopped = True

    def enable(self):
        self.stopped = False

    def stop_confirmed(self):
        return self.stopped

    def enabled_confirmed(self):
        return not self.stopped

    def close(self):
        self.stop()


class SimLeader:
    def __init__(self, cfg):
        self.q = [0.0] * 14
        self.manual = True
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            return self.q.copy(), time.monotonic()

    def feedback(self, target):
        with self.lock:
            self.q = vector(target)

    def set_manual(self, manual):
        self.manual = manual

    def close(self):
        pass


class RosIO:
    def __init__(self, domain, name, history_seconds=0.3):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from sensor_msgs.msg import JointState
        from std_msgs.msg import Bool, Int32

        self.rclpy, self.JointState, self.Bool, self.Int32 = rclpy, JointState, Bool, Int32
        self.context = Context()
        # A process can contain both interfaces. Do not change the process-wide
        # ROS_DOMAIN_ID while another context or ROS client is running.
        rclpy.init(context=self.context, domain_id=domain)
        self.node = rclpy.create_node(name, context=self.context)
        self.qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE
        )
        # Legacy ARX and OpenEAI subscribers request RELIABLE. A BEST_EFFORT
        # command publisher never matches those subscribers in ROS 2.
        self.command_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self.lock = threading.RLock()
        self.states, self.modes = {}, {}
        self.history_seconds = history_seconds
        self.history = {side: deque() for side in ("left", "right")}
        self.executor = MultiThreadedExecutor(context=self.context, num_threads=2)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def subscribe_state(self, key, topic, names, gripper_scale=1):
        def receive(msg):
            try:
                if len(msg.name) != len(msg.position) or len(set(msg.name)) != len(msg.name):
                    raise Rejected("INVALID_JOINT_NAMES")
                values = dict(zip(msg.name, msg.position))
                q = vector([values[n] for n in names], 7)
                with self.lock:
                    stamp = time.monotonic()
                    self.states[key] = (q, stamp)
                    history = self.history[key]
                    history.append((q[:6] + [q[6] * gripper_scale], stamp))
                    while history and stamp - history[0][1] > self.history_seconds:
                        history.popleft()
            except (ValueError, KeyError):
                with self.lock:
                    self.states.pop(key, None)

        self.node.create_subscription(self.JointState, topic, receive, self.qos)

    def mode_type(self, message_type):
        return {"joint_state": self.JointState, "bool": self.Bool, "int32": self.Int32}[message_type]

    def mode_message(self, arm, manual):
        value = arm.mode_mit_value if manual else arm.mode_position_value
        msg = self.mode_type(arm.mode_message_type)()
        if arm.mode_message_type == "joint_state":
            # OpenEAI also requires the end-effector mode; zero selects joints.
            msg.position = [float(value), 0.0]
        else:
            msg.data = bool(value) if arm.mode_message_type == "bool" else int(value)
        return msg

    def subscribe_mode(self, key, topic, is_bool=False, *, message_type=None):
        message_type = message_type or ("bool" if is_bool else "joint_state")

        def receive(msg):
            value = msg.position[0] if message_type == "joint_state" and msg.position else None
            if message_type != "joint_state":
                value = msg.data
            try:
                value = vector([float(value)], 1)[0]
            except (ValueError, TypeError):
                value = None
            with self.lock:
                self.modes[key] = (value, time.monotonic())

        self.node.create_subscription(self.mode_type(message_type), topic, receive, 10)

    def read(self):
        with self.lock:
            if any(side not in self.states for side in ("left", "right")):
                raise Rejected("JOINT_STATE_UNAVAILABLE")
            return self.states["left"][0] + self.states["right"][0], min(
                self.states[n][1] for n in ("left", "right")
            )

    def stationary(self, window=0.2, joint_tolerance=0.01, gripper_tolerance=0.002, min_samples=3):
        """Measured stationarity only; this is not a drive/brake acknowledgement."""
        now = time.monotonic()
        with self.lock:
            for history in self.history.values():
                samples = [(q, stamp) for q, stamp in history if now - stamp <= window + 0.05]
                if len(samples) < min_samples or samples[-1][1] - samples[0][1] < window:
                    return False
                for index, tolerance in enumerate([joint_tolerance] * 6 + [gripper_tolerance]):
                    values = [q[index] for q, _ in samples]
                    if max(values) - min(values) > tolerance:
                        return False
            return True

    def close(self):
        self.executor.shutdown(timeout_sec=2)
        self.node.destroy_node()
        self.rclpy.shutdown(context=self.context)
        self.thread.join(timeout=2)


def configure_modes(adapter, arms, *, prefix=""):
    """Subscribe to optional driver mode contracts without publishing on boot."""
    adapter.mode_pubs = {}
    for arm in arms:
        if not arm.mode_switch_enabled:
            continue
        if arm.mode_confirmation == "topic" and arm.mode_state_topic:
            adapter.io.subscribe_mode(
                prefix + arm.name, arm.mode_state_topic, message_type=arm.mode_message_type
            )
        adapter.mode_pubs[arm.name] = adapter.io.node.create_publisher(
            adapter.io.mode_type(arm.mode_message_type), arm.mode_topic, 10
        )


def mode_confirmed(adapter, arm, manual, started, target_topic, *, prefix=""):
    if arm.mode_confirmation == "topic":
        desired = arm.mode_mit_value if manual else arm.mode_position_value
        with adapter.io.lock:
            value, stamp = adapter.io.modes.get(prefix + arm.name, (None, 0))
        return (
            value == desired
            and stamp >= started
            and time.monotonic() - stamp < adapter.cfg.safety.driver_ack_max_age_ms / 1000
        )
    # In the inspected OpenEAI implementation only MIT_DRAG creates a target
    # echo publisher. ctrl_mode's static ROS parameter is not an acknowledgement.
    node = adapter.io.node
    targets = node.get_publishers_info_by_topic(target_topic)
    subscribers = node.get_subscriptions_info_by_topic(target_topic)
    drivers = [info for info in subscribers if info.node_name == arm.driver_node]
    echo = [info for info in targets if info.node_name == arm.driver_node]
    with adapter.io.lock:
        _, stamp = adapter.io.states.get(arm.name, (None, 0))
    return (
        len(drivers) == 1
        and bool(echo) == manual
        and len(echo) <= 1
        and stamp >= started + adapter.cfg.safety.leader_mode_settle_seconds
        and (
            not adapter.cfg.safety.checks.state_freshness
            or time.monotonic() - stamp <= adapter.cfg.safety.state_max_age_ms / 1000
        )
    )


def transition_modes(adapter, arms, manual, *, confirmation, prefix="", error="DRIVER_MODE_TIMEOUT"):
    arms = [arm for arm in arms if arm.mode_switch_enabled]
    if not arms:
        return
    started = time.monotonic()
    deadline = started + adapter.cfg.safety.transition_timeout_seconds / 2
    while not adapter.cfg.safety.checks.transition_timeout or time.monotonic() < deadline:
        for arm in arms:
            adapter.mode_pubs[arm.name].publish(adapter.io.mode_message(arm, manual))
        if not confirmation or all(
            mode_confirmed(
                adapter,
                arm,
                manual,
                started,
                arm.target_topic if hasattr(arm, "target_topic") else arm.command_topic,
                prefix=prefix,
            )
            for arm in arms
        ):
            return
        time.sleep(0.02)
    raise Rejected(error)


class RosRobot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.io = RosIO(
            cfg.robot.ros_domain_id, "remote_teleop_robot", cfg.safety.stationary_window_seconds + 0.1
        )
        self.targets, self.stops = {}, {}
        self.stopped = True
        self.last_stop_request = 0.0
        for arm in cfg.robot.arms:
            self.io.subscribe_state(arm.name, arm.state_topic, arm.joint_names, arm.gripper_feedback_scale)
            self.io.subscribe_mode(arm.name, arm.stop_state_topic, True)
            self.targets[arm.name] = self.io.node.create_publisher(
                self.io.JointState, arm.command_topic, self.io.command_qos
            )
            self.stops[arm.name] = self.io.node.create_publisher(self.io.Bool, arm.stop_topic, 10)
        configure_modes(self, cfg.robot.arms, prefix="control:")

    def read(self):
        q, stamp = self.io.read()
        # Put gripper feedback in the same coordinate space as target commands.
        for i, arm in enumerate(self.cfg.robot.arms):
            q[7 * i + 6] = q[7 * i + 6] * arm.gripper_feedback_scale + arm.gripper_feedback_offset
        return q, stamp

    def _stopping(self, stopped):
        if self.stopped != stopped or self.last_stop_request == 0:
            self.last_stop_request = time.monotonic()
        self.stopped = stopped
        for publisher in self.stops.values():
            msg = self.io.Bool()
            msg.data = stopped
            publisher.publish(msg)

    def stop(self):
        self._stopping(True)

    def enable(self):
        self._enter_position_mode()
        self._stopping(False)

    def _enter_position_mode(self, current=None):
        if not any(arm.mode_switch_enabled for arm in self.cfg.robot.arms):
            return
        if current is None:
            current, stamp = self.read()
            if (
                self.cfg.safety.checks.state_freshness
                and time.monotonic() - stamp > self.cfg.safety.state_max_age_ms / 1000
            ):
                raise Rejected("STALE_JOINT_STATE")
        # Position mode must never activate an old driver target. This is an
        # explicit enable action; adapter construction remains passive.
        self._publish(current)
        transition_modes(
            self,
            self.cfg.robot.arms,
            False,
            confirmation=self.cfg.safety.checks.enable_confirmation,
            prefix="control:",
        )

    def _confirmed(self, value):
        with self.io.lock:
            return all(
                n in self.io.modes
                and self.io.modes[n][0] == value
                and self.io.modes[n][1] >= self.last_stop_request
                and time.monotonic() - self.io.modes[n][1] < self.cfg.safety.driver_ack_max_age_ms / 1000
                for n in ("left", "right")
            )

    def stop_confirmed(self):
        return self.stopped and (not self.cfg.safety.checks.stop_confirmation or self._confirmed(True))

    def enabled_confirmed(self):
        return not self.stopped and (not self.cfg.safety.checks.enable_confirmation or self._confirmed(False))

    def command(self, target):
        q = vector(target)
        if not self.enabled_confirmed():
            raise Rejected("DRIVER_NOT_ENABLED")
        _, stamp = self.read()
        if (
            self.cfg.safety.checks.state_freshness
            and time.monotonic() - stamp > self.cfg.safety.state_max_age_ms / 1000
        ):
            raise Rejected("STALE_JOINT_STATE")
        self._publish(q)

    def _publish(self, target):
        for i, arm in enumerate(self.cfg.robot.arms):
            msg = self.io.JointState()
            msg.header.stamp = self.io.node.get_clock().now().to_msg()
            msg.name, msg.position = arm.joint_names, target[i * 7 : (i + 1) * 7]
            self.targets[arm.name].publish(msg)

    def close(self):
        self.stop()
        self.io.close()


class PositionHoldRobot(RosRobot):
    """Compatibility with the existing ARX JointState bridge.

    No drive-stop acknowledgement exists on that bridge. On an owned motion's
    stop, capture fresh measured joints once and hold that target. Confirmation
    means measured stationarity, never torque-off or a safety-rated stop. Boot
    and passive monitoring do not publish any joint command.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.io = RosIO(
            cfg.robot.ros_domain_id, "remote_teleop_robot", cfg.safety.stationary_window_seconds + 0.1
        )
        self.lock = threading.RLock()
        self.stopped = True
        self.hold_target = None
        self.last_target = None
        self.targets = {}
        for arm in cfg.robot.arms:
            self.io.subscribe_state(arm.name, arm.state_topic, arm.joint_names, arm.gripper_feedback_scale)
            self.targets[arm.name] = self.io.node.create_publisher(
                self.io.JointState, arm.command_topic, self.io.command_qos
            )
        configure_modes(self, cfg.robot.arms, prefix="control:")

    def _fresh(self):
        try:
            q, stamp = self.read()
            return (
                q
                if not self.cfg.safety.checks.state_freshness
                or time.monotonic() - stamp <= self.cfg.safety.state_max_age_ms / 1000
                else None
            )
        except Rejected:
            return None

    def _publish(self, target):
        for i, arm in enumerate(self.cfg.robot.arms):
            msg = self.io.JointState()
            msg.header.stamp = self.io.node.get_clock().now().to_msg()
            msg.name, msg.position = arm.joint_names, target[7 * i : 7 * (i + 1)]
            self.targets[arm.name].publish(msg)

    def enable(self):
        with self.lock:
            current = self._fresh()
            if current is None:
                raise Rejected("STALE_JOINT_STATE")
            self._enter_position_mode(current)
            self.stopped = False
            self.hold_target = None

    def stop(self):
        with self.lock:
            if not self.stopped and self.last_target is not None:
                # With stale feedback retain the last bounded command, never zero.
                measured = self._fresh()
                if measured is not None and any(
                    not self.cfg.safety.joint_min[i % 7] <= value <= self.cfg.safety.joint_max[i % 7]
                    for i, value in enumerate(measured)
                    if i % 7 != 6
                ):
                    measured = None
                self.hold_target = measured or self.last_target.copy()
                # ARX gripper feedback and command units are not necessarily the
                # same. Preserve the last applied command when holding the arm.
                for index in (6, 13):
                    self.hold_target[index] = self.last_target[index]
            self.stopped = True
            if self.hold_target is not None:
                self._publish(self.hold_target)

    def stop_confirmed(self):
        with self.lock:
            s = self.cfg.safety
            return (
                self.stopped
                and self._fresh() is not None
                and (
                    not s.checks.stop_confirmation
                    or self.io.stationary(
                        s.stationary_window_seconds,
                        s.stationary_joint_tolerance,
                        s.stationary_gripper_tolerance,
                        s.stationary_min_samples,
                    )
                )
            )

    def enabled_confirmed(self):
        return (
            not self.stopped
            and self._fresh() is not None
            and (
                not self.cfg.safety.checks.single_command_subscriber
                or all(publisher.get_subscription_count() == 1 for publisher in self.targets.values())
            )
        )

    def command(self, target):
        with self.lock:
            if not self.enabled_confirmed():
                raise Rejected("DRIVER_NOT_ENABLED")
            self.last_target = vector(target)
            self._publish(self.last_target)


class RosLeader:
    """Configurable JointState leader, with optional MIT/position switching.

    Joint mappings and ROS domains belong to the leader independently of the
    slave. Optional physical mode control is independent of logical feedback
    ownership; drivers without a mode channel manage their own control mode.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.io = RosIO(cfg.leader.ros_domain_id, "remote_teleop_leader")
        self.targets, self.mode_pubs = {}, {}
        self.manual = True
        for arm in cfg.leader.arms:
            self.io.subscribe_state(arm.name, arm.state_topic, arm.joint_names, arm.gripper_feedback_scale)
            self.targets[arm.name] = self.io.node.create_publisher(
                self.io.JointState, arm.target_topic, self.io.command_qos
            )
        configure_modes(self, cfg.leader.arms)

    def read(self):
        raw, stamp = self.io.read()
        result = []
        for i, arm in enumerate(self.cfg.leader.arms):
            values = raw[i * 7 : (i + 1) * 7]
            values[6] = values[6] * arm.gripper_feedback_scale + arm.gripper_feedback_offset
            result.extend(v * d + o for v, d, o in zip(values, arm.directions, arm.offsets))
        return result, stamp

    def _publish(self, values):
        for i, arm in enumerate(self.cfg.leader.arms):
            msg = self.io.JointState()
            msg.name = arm.joint_names
            msg.header.stamp = self.io.node.get_clock().now().to_msg()
            msg.position = [
                (v - o) / d for v, o, d in zip(values[i * 7 : (i + 1) * 7], arm.offsets, arm.directions)
            ]
            self.targets[arm.name].publish(msg)

    def feedback(self, values):
        if self.manual:
            raise Rejected("LEADER_MANUAL")
        self._publish(vector(values))

    def set_manual(self, manual):
        current, stamp = self.read()
        if (
            self.cfg.safety.checks.state_freshness
            and time.monotonic() - stamp > self.cfg.safety.state_max_age_ms / 1000
        ):
            raise Rejected("STALE_LEADER_STATE")
        if not manual:
            self._publish(current)
        transition_modes(
            self,
            self.cfg.leader.arms,
            manual,
            confirmation=self.cfg.safety.checks.leader_mode_confirmation,
            error="LEADER_MODE_TIMEOUT",
        )
        self.manual = manual

    def _mode_confirmed(self, arm, manual, started):
        return mode_confirmed(self, arm, manual, started, arm.target_topic)

    def close(self):
        # Do not silently disable torque or request a reset trajectory on shutdown.
        self.io.close()


class SerialLeader:
    def __init__(self, cfg):
        from remote_teleop.components.teleoperators.sii_arm_leader.config_sii_arm_leader import (
            SiiArmLeaderArmConfig,
            SiiArmLeaderConfig,
        )
        from remote_teleop.components.teleoperators.sii_arm_leader.sii_arm_leader import SiiArmLeader

        self.cfg = cfg
        arms = {
            a.name: SiiArmLeaderArmConfig(
                port=a.port,
                baudrate=a.baudrate,
                motor_ids=a.motor_ids,
                directions=a.directions,
                joint_offsets=a.offsets[:6],
                gripper_output_scale=a.gripper_scale,
                gripper_output_offset=a.gripper_offset,
                gripper_max_delta_deg=a.gripper_range_degrees,
                disable_torque_on_disconnect=False,
            )
            for a in cfg.leader.arms
        }
        # Require the same connect pose as a deliberately saved calibration, never silently re-zero.
        path = Path(cfg.leader.calibration_file)
        if not path.is_file():
            raise Rejected("SERIAL_CALIBRATION_REQUIRED")
        import json

        reference = json.loads(path.read_text())
        reference = {name: vector(reference[name], 7) for name in ("left", "right")}
        self.device = SiiArmLeader(SiiArmLeaderConfig(arms=arms))
        self.manual = True
        self.lock = threading.RLock()
        try:
            self.device.connect()
            for name, arm in self.device._arms.items():
                if (
                    cfg.safety.checks.alignment
                    and max(abs(a - b) for a, b in zip(arm._reference_joint_angles, reference[name]))
                    > cfg.safety.alignment_tolerance
                ):
                    raise Rejected("SERIAL_REFERENCE_MISMATCH")
        except Exception:
            for arm in self.device._arms.values():
                if arm.bus.is_connected:
                    arm.disconnect()
            raise

    def read(self):
        with self.lock:
            values = self.device.get_action()
            return vector([values[n] for n in self.cfg.joint_names]), time.monotonic()

    def set_manual(self, manual):
        with self.lock:
            if not manual:
                # Preload current targets BEFORE torque-on to avoid the legacy stale-goal jump.
                for arm in self.device._arms.values():
                    raw = arm.bus.sync_read("Present_Position", normalize=False, num_retry=2)
                    vector(list(raw.values()), 7)
                    arm.bus.sync_write("Goal_Position", raw, normalize=False)
            self.device.set_manual_control(manual)
            if self.cfg.safety.checks.leader_mode_confirmation:
                for arm in self.device._arms.values():
                    values = arm.bus.sync_read("Torque_Enable", normalize=False, num_retry=2)
                    if len(values) != 7 or not all(v == int(not manual) for v in values.values()):
                        raise Rejected("LEADER_MODE_TIMEOUT")
            self.manual = manual

    def feedback(self, values):
        with self.lock:
            if self.manual:
                raise Rejected("LEADER_MANUAL")
            self.device.send_feedback(dict(zip(self.cfg.joint_names, vector(values))))

    def close(self):
        with self.lock:
            self.device.disconnect()


def robot_adapter(cfg):
    if cfg.robot.backend == "sim":
        return SimRobot(cfg)
    return PositionHoldRobot(cfg) if cfg.robot.stop_strategy == "position_hold" else RosRobot(cfg)


def leader_adapter(cfg):
    return {"sim": SimLeader, "ros": RosLeader, "sii_serial": SerialLeader}[cfg.leader.backend](cfg)
