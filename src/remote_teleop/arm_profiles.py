"""Built-in arm templates; each read returns independent, editable copies.

These are wiring examples, not deployment commissioning assertions. In
particular, selecting the printed leader does not validate its passive support.
"""

from .config import (
    BUILTIN_ARM_PROFILE_IDS,
    Arm,
    Homing,
    Leader,
    LeaderArm,
    LeaderProfile,
    Robot,
    RobotProfile,
)


def builtin_arm_profiles() -> dict:
    eai_robot = Robot(
        backend="ros",
        driver="eaiarm",
        stop_strategy="position_hold",
        arms=[
            Arm(
                name=side,
                state_topic=f"/{side}/joint_states",
                command_topic=f"/{side}/joint_targets",
                mode_switch_enabled=True,
                mode_topic=f"/{side}/set_control_mode",
                mode_confirmation="openeai_graph",
            )
            for side in ("left", "right")
        ],
    )
    arx_robot = Robot(
        backend="ros",
        driver="arx5",
        stop_strategy="position_hold",
        arms=[
            Arm(
                name=side,
                state_topic=f"/{side}/ARX_joint_states",
                command_topic=f"/{side}/ARX_joint_targets",
                gripper_feedback_scale=0.02,
                mode_switch_enabled=False,
            )
            for side in ("left", "right")
        ],
    )
    eai_leader = Leader(
        backend="ros",
        driver="eaiarm",
        homing=Homing(reset_topic="/robot_reset", reset_prepare_topic="/robot_prepare"),
        passive_supported=False,
        arms=[
            LeaderArm(
                name=side,
                state_topic=f"/{side}/joint_states",
                target_topic=f"/{side}/joint_targets",
                mode_switch_enabled=True,
                mode_topic=f"/master/set_control_mode_{side}",
                mode_confirmation="openeai_graph",
            )
            for side in ("left", "right")
        ],
    )
    printed_leader = Leader(
        backend="sii_serial",
        passive_supported=False,
        serial_support_validated=False,
        arms=[
            LeaderArm(
                name=side,
                state_topic="",
                target_topic="",
                mode_switch_enabled=False,
                port=f"/dev/ttyACM{index + 2}",
            )
            for index, side in enumerate(("left", "right"))
        ],
    )
    return {
        "robot": [
            RobotProfile(
                id=BUILTIN_ARM_PROFILE_IDS["robot"][0], name="EAIArm · ROS JointState", config=eai_robot
            ).model_dump(),
            RobotProfile(
                id=BUILTIN_ARM_PROFILE_IDS["robot"][1], name="ARX5 · ROS JointState bridge", config=arx_robot
            ).model_dump(),
        ],
        "leader": [
            LeaderProfile(
                id=BUILTIN_ARM_PROFILE_IDS["leader"][0], name="EAIArm · ROS", config=eai_leader
            ).model_dump(),
            LeaderProfile(
                id=BUILTIN_ARM_PROFILE_IDS["leader"][1], name="3D 打印遥操臂 · 串口", config=printed_leader
            ).model_dump(),
        ],
    }
