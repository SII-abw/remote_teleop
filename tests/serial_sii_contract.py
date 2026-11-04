#!/usr/bin/env python

# Copyright 2026 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from remote_teleop.components.teleoperators.sii_arm_leader import SiiArmLeader, SiiArmLeaderConfig


def _make_bus_mock() -> MagicMock:
    bus = MagicMock(name="FeetechBusMock")
    bus.is_connected = False
    bus.is_calibrated = True

    def _connect(*_args, **_kwargs):
        bus.is_connected = True

    def _disconnect(_disable=True):
        bus.is_connected = False

    bus.connect.side_effect = _connect
    bus.disconnect.side_effect = _disconnect
    bus.model_resolution_table = {"sts3215": 360}
    bus.set_baudrate.return_value = None
    bus.configure_motors.return_value = None
    bus.disable_torque.return_value = None
    bus.enable_torque.return_value = None
    bus.write.return_value = None
    bus.sync_write.return_value = None
    return bus


@pytest.fixture
def leader(tmp_path):
    bus_mock = _make_bus_mock()

    def _bus_side_effect(*_args, **kwargs):
        bus_mock.motors = kwargs["motors"]
        bus_mock.sync_read.return_value = {motor_name: 0.0 for motor_name in bus_mock.motors}
        return bus_mock

    with patch(
        "remote_teleop.components.teleoperators.sii_arm_leader.sii_arm_leader.FeetechMotorsBus",
        side_effect=_bus_side_effect,
    ):
        teleop = SiiArmLeader(
            SiiArmLeaderConfig(
                port="/dev/null",
                calibration_dir=tmp_path,
                require_motor_check=False,
                smoothing=0.0,
            )
        )
        yield teleop
        if teleop.is_connected:
            teleop.disconnect()


def test_get_action_matches_gello_delta_mapping(leader):
    leader.connect()

    deltas_rad = np.array([0.10, 0.20, -0.30, 0.05, -0.15, 0.25, 0.10], dtype=np.float32)
    raw_positions = {
        motor_name: float(np.rad2deg(delta))
        for motor_name, delta in zip(leader.config.motor_names, deltas_rad, strict=True)
    }
    leader.bus.sync_read.return_value = raw_positions

    action = leader.get_action()

    expected_arm = [
        -deltas_rad[0],
        -deltas_rad[1],
        -0.30,
        0.05,
        0.15,
        -0.25,
    ]
    for joint_name, expected_value in zip(leader.config.joint_names[:6], expected_arm, strict=True):
        assert action[f"{joint_name}.pos"] == pytest.approx(expected_value, abs=1e-3)

    expected_gripper = 0.085 * np.clip(np.degrees(abs(deltas_rad[-1])) / 40.0, 0.0, 1.0) - 0.005
    assert action["gripper.pos"] == pytest.approx(expected_gripper, abs=1e-3)


def test_get_action_is_continuous_across_half_turn(leader):
    leader.connect()
    raw_positions = {motor_name: 0.0 for motor_name in leader.config.motor_names}

    raw_positions["elbow_pitch"] = 179.0
    leader.bus.sync_read.return_value = raw_positions.copy()
    before = leader.get_action()["joint_3.pos"]

    raw_positions["elbow_pitch"] = 181.0
    leader.bus.sync_read.return_value = raw_positions
    after = leader.get_action()["joint_3.pos"]

    assert before == pytest.approx(np.deg2rad(179.0), abs=1e-3)
    assert after == pytest.approx(np.deg2rad(181.0), abs=1e-3)
    assert after - before == pytest.approx(np.deg2rad(2.0), abs=1e-3)


def test_send_feedback_writes_goal_positions(leader):
    leader.connect()

    feedback = {
        "joint_1.pos": 0.12,
        "joint_2.pos": -0.08,
        "joint_3.pos": 0.05,
        "joint_4.pos": -0.02,
        "joint_5.pos": 0.04,
        "joint_6.pos": 0.03,
        "gripper.pos": 0.04,
    }
    leader.send_feedback(feedback)

    goal_positions = leader.bus.sync_write.call_args.args[1]
    assert goal_positions["base_yaw"] == round(np.degrees(-0.12))
    assert goal_positions["shoulder_pitch"] == round(np.degrees(0.08))
    assert goal_positions["elbow_pitch"] == round(np.degrees(0.05))
    assert goal_positions["forearm_roll"] == round(np.degrees(-0.02))
    assert goal_positions["wrist_roll"] == round(np.degrees(-0.03))
    assert "gripper" in goal_positions


def test_send_feedback_does_not_wrap_raw_goal_at_one_turn(leader):
    leader.connect()
    runtime = next(iter(leader._arms.values()))
    runtime._reference_joint_angles[2] = np.deg2rad(350.0)

    leader.send_feedback({"joint_3.pos": np.deg2rad(20.0)})

    goal_positions = leader.bus.sync_write.call_args.args[1]
    assert goal_positions["elbow_pitch"] == 370


def test_reference_feedback_pose_targets_connect_reference(leader):
    leader.connect()
    runtime = next(iter(leader._arms.values()))
    reference_degrees = np.array([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0], dtype=np.float32)
    runtime._reference_joint_angles = np.deg2rad(reference_degrees).astype(np.float32)

    feedback = leader.get_reference_feedback_pose()

    for joint_name in leader.config.joint_names[:6]:
        assert feedback[f"{joint_name}.pos"] == pytest.approx(0.0)
    assert feedback["gripper.pos"] == pytest.approx(leader.config.gripper_output_offset)

    leader.send_feedback(feedback)

    goal_positions = leader.bus.sync_write.call_args.args[1]
    for motor_name, expected_degree in zip(leader.config.motor_names, reference_degrees, strict=True):
        assert goal_positions[motor_name] == round(float(expected_degree))


def test_capture_reference_pose_resets_arm_delta(leader):
    leader.connect()

    raw_positions = {
        motor_name: float(np.rad2deg(delta))
        for motor_name, delta in zip(
            leader.config.motor_names,
            np.array([0.10, -0.20, 0.30, -0.05, 0.15, -0.25, 0.10], dtype=np.float32),
            strict=True,
        )
    }
    leader.bus.sync_read.return_value = raw_positions

    before = leader.get_action()
    assert before["joint_1.pos"] != pytest.approx(0.0)

    leader.capture_reference_pose()
    after = leader.get_action()

    for joint_name in leader.config.joint_names[:6]:
        assert after[f"{joint_name}.pos"] == pytest.approx(0.0, abs=1e-3)
