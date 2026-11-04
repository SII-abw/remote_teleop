"""Validated deployment configuration; secrets are references, never UI values."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import fcntl
from copy import deepcopy
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class General(Model):
    robot_id: str = Field(default="dual-arm-01", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    simulation: bool = True
    operation_mode: Literal["teleop", "hil"] = "hil"
    task: str = Field(default="fold the shirt fast", min_length=1, max_length=1000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Network(Model):
    gateway_url: str = "ws://127.0.0.1:8800"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8800, ge=1024, le=65535)
    ca_file: str = ""
    certificate_file: str = ""
    private_key_file: str = ""
    reconnect_seconds: float = Field(default=1, ge=0.1, le=30)
    rpc_timeout_seconds: float = Field(default=8, ge=0.5, le=60)
    telemetry_hz: int = Field(default=100, ge=1, le=100)
    observation_hz: int = Field(default=100, ge=1, le=100)
    max_message_bytes: int = Field(default=4194304, ge=65536, le=16777216)
    webrtc_enabled: bool = True
    ice_servers: list[str] = Field(default_factory=list)
    turn_username_env: str = "TELEOP_TURN_USERNAME"
    turn_password_env: str = "TELEOP_TURN_PASSWORD"


class Security(Model):
    credentials_file: str = "state/credentials.json"
    token_env: str = ""  # Optional override for the current process's role.
    allow_loopback_http: bool = True
    ui_session_seconds: int = Field(default=3600, ge=60, le=43200)
    allowed_origins: list[str] = Field(default_factory=list)


class AttendedTest(Model):
    """Manual commissioning controls; None disables an individual limit."""

    enabled: bool = False
    expires_at: float | None = Field(default=0, ge=0)
    duration_seconds: float | None = Field(default=120, gt=0)
    max_start_window_seconds: float | None = Field(default=900, gt=0)
    single_use: bool = True
    joint_delta: float | None = Field(default=0.05, gt=0)
    joint_feedback_margin: float = Field(default=0.005, ge=0)
    gripper_commands: list[float] | None = Field(default_factory=lambda: [0.0, 0.0])
    gripper_baseline_limit: float | None = Field(default=0.05, gt=0)
    gripper_feedback_delta: float | None = Field(default=0.02, gt=0)
    velocity_cap: float | None = Field(default=0.05, gt=0)
    acceleration_cap: float | None = Field(default=0.1, gt=0)
    allow_recording: bool = False


class MotionChecks(Model):
    """Operator-configurable motion checks, shown together in the danger zone."""

    hardware_validation: bool = True
    position_limits: bool = True
    velocity_limits: bool = True
    acceleration_limits: bool = True
    tracking_error: bool = True
    command_timeout: bool = True
    control_loop_watchdog: bool = True
    state_freshness: bool = True
    command_freshness: bool = True
    observation_freshness: bool = True
    leader_heartbeat: bool = True
    operator_heartbeat: bool = True
    alignment: bool = True
    transition_timeout: bool = True
    stop_confirmation: bool = True
    enable_confirmation: bool = True
    leader_mode_confirmation: bool = True
    single_command_subscriber: bool = True


class Safety(Model):
    checks: MotionChecks = Field(default_factory=MotionChecks)
    control_hz: int = Field(default=30, ge=20, le=250)
    watchdog_ms: int = Field(default=250, gt=0)
    watchdog_check_ms: int = Field(default=10, ge=2, le=50)
    state_max_age_ms: int = Field(default=150, gt=0)
    camera_max_age_ms: int = Field(default=300, gt=0)
    observation_max_age_ms: int = Field(default=500, gt=0)
    command_max_age_ms: int = Field(default=150, gt=0)
    transition_timeout_seconds: float = Field(default=5, gt=0)
    leader_lease_ms: int = Field(default=500, gt=0)
    operator_lease_seconds: float = Field(default=3, gt=0)
    alignment_tolerance: float = Field(default=0.12, gt=0)
    tracking_error_limit: float = Field(default=0.5, gt=0)
    tracking_error_duration_ms: int = Field(default=300, ge=0, le=2000)
    tracking_error_hard_limit: float = Field(default=1.0, gt=0)
    driver_enable_timeout_seconds: float = Field(default=0.5, gt=0)
    driver_ack_max_age_ms: int = Field(default=500, gt=0)
    stationary_window_seconds: float = Field(default=0.2, gt=0)
    stationary_joint_tolerance: float = Field(default=0.01, ge=0)
    stationary_gripper_tolerance: float = Field(default=0.002, ge=0)
    stationary_min_samples: int = Field(default=3, ge=2)
    leader_mode_settle_seconds: float = Field(default=0.15, ge=0)
    max_control_dt_periods: float = Field(default=2, ge=1)
    require_cameras: bool = True
    hardware_validated: bool = False
    attended_test: AttendedTest = Field(default_factory=AttendedTest)
    joint_min: list[float] = Field(default_factory=lambda: [-3.14] * 6 + [0.0])
    joint_max: list[float] = Field(default_factory=lambda: [3.14] * 6 + [0.085])
    velocity_max: list[float] = Field(default_factory=lambda: [0.5] * 6 + [0.08])
    acceleration_max: list[float] = Field(default_factory=lambda: [1.0] * 6 + [0.2])


class RosArmFields(Model):
    """Common JointState position contract, with an optional mode channel."""

    joint_names: list[str] = Field(default_factory=lambda: [f"joint_{i}" for i in range(1, 8)])
    gripper_feedback_scale: float = Field(default=1, gt=0)
    gripper_feedback_offset: float = 0
    mode_switch_enabled: bool = False
    mode_topic: str = ""
    mode_state_topic: str = ""
    mode_confirmation: Literal["topic", "openeai_graph"] = "topic"
    mode_message_type: Literal["joint_state", "bool", "int32"] = "joint_state"
    mode_position_value: float = 0
    mode_mit_value: float = 1
    driver_node: str = "OpenEAIArm_node"

    @model_validator(mode="after")
    def joint_mapping(self):
        if (
            len(self.joint_names) != 7
            or len(set(self.joint_names)) != 7
            or any(not name.strip() for name in self.joint_names)
        ):
            raise ValueError("each arm needs seven unique nonempty joint names")
        return self

    def validate_ros(self, command_topic: str):
        if not self.state_topic.strip() or not command_topic.strip():
            raise ValueError("ROS arms require state and joint position target topics")
        if not self.mode_switch_enabled:
            return
        if not self.mode_topic.strip():
            raise ValueError("enabled ROS mode switching requires a mode command topic")
        if self.mode_position_value == self.mode_mit_value:
            raise ValueError("position and MIT mode values must differ")
        values = (self.mode_position_value, self.mode_mit_value)
        if self.mode_message_type == "bool" and any(value not in (0, 1) for value in values):
            raise ValueError("Bool mode values must be 0 or 1")
        if self.mode_message_type == "int32" and any(
            value != int(value) or not -(2**31) <= value < 2**31 for value in values
        ):
            raise ValueError("Int32 mode values must be 32-bit integers")
        if self.mode_confirmation == "openeai_graph":
            if not self.driver_node.strip():
                raise ValueError("OpenEAI graph confirmation requires a driver node")
            if self.mode_message_type != "joint_state" or values != (0, 1):
                raise ValueError("OpenEAI graph confirmation requires JointState modes position=0 and MIT=1")


class Arm(RosArmFields):
    name: Literal["left", "right"]
    state_topic: str
    command_topic: str
    stop_topic: str = ""  # std_msgs/Bool, latched logical safety input to the driver.
    stop_state_topic: str = ""  # std_msgs/Bool acknowledgement from the driver.


class Homing(Model):
    reset_topic: str = Field(
        default="",
        description="可选 std_msgs/Int32：1 开始归零、0 退出并保持。留空使用平滑位置目标序列；原生 reset 的运动限速由驱动负责。",
    )
    reset_prepare_topic: str = ""  # Optional Int32(0) clears a driver's prepare mode.
    reset_expected_subscribers: int = Field(default=2, ge=1, le=16)
    duration_seconds: float = Field(
        default=5, ge=1, le=120, description="仅用于位置目标序列；必要时按速度、加速度限额延长。"
    )
    timeout_seconds: float = Field(default=120, ge=5, le=300)
    joint_tolerance: float = Field(default=0.05, gt=0, le=0.1)
    gripper_tolerance: float = Field(default=0.01, gt=0, le=0.02)
    confirm_samples: int = Field(default=3, ge=2, le=20)
    velocity_max: list[float] = Field(
        default_factory=lambda: [0.25] * 6 + [0.02],
        description="仅用于位置目标序列：六个关节 rad/s，加一个夹爪控制单位/s。",
    )
    acceleration_max: list[float] = Field(
        default_factory=lambda: [0.5] * 6 + [0.05],
        description="仅用于位置目标序列；与 safety 限额取较小值，回零始终限速、限加速度。",
    )

    @model_validator(mode="after")
    def limits(self):
        if any(len(values) != 7 for values in (self.velocity_max, self.acceleration_max)):
            raise ValueError("homing limits need seven entries")
        if any(value <= 0 for value in self.velocity_max + self.acceleration_max):
            raise ValueError("homing velocity and acceleration limits must be positive")
        if self.timeout_seconds <= self.duration_seconds:
            raise ValueError("homing timeout must exceed minimum duration")
        if self.reset_topic and not self.reset_topic.strip():
            raise ValueError("reset topic cannot contain only whitespace")
        if self.reset_prepare_topic and not self.reset_prepare_topic.strip():
            raise ValueError("prepare topic cannot contain only whitespace")
        return self


class Robot(Model):
    backend: Literal["sim", "ros"] = "sim"
    driver: Literal["eaiarm", "arx5"] = "arx5"
    stop_strategy: Literal["driver_ack", "position_hold"] = "driver_ack"
    arms: list[Arm] = Field(
        default_factory=lambda: [
            Arm(name=n, state_topic=f"/{n}/ARX_joint_states", command_topic=f"/{n}/ARX_joint_targets")
            for n in ("left", "right")
        ]
    )
    ros_domain_id: int = Field(default=0, ge=0, le=232)
    startup_timeout_seconds: float = Field(default=10, ge=1, le=60)
    homing: Homing = Field(default_factory=Homing)

    @model_validator(mode="after")
    def arm_contract(self):
        if [arm.name for arm in self.arms] != ["left", "right"]:
            raise ValueError("arms must be ordered left, right")
        if self.backend == "ros":
            for arm in self.arms:
                arm.validate_ros(arm.command_topic)
                if (
                    arm.mode_switch_enabled
                    and arm.mode_confirmation == "openeai_graph"
                    and self.driver != "eaiarm"
                ):
                    raise ValueError("OpenEAI graph confirmation requires the eaiarm driver")
        return self


class LeaderArm(RosArmFields):
    name: Literal["left", "right"]
    state_topic: str
    target_topic: str
    mode_switch_enabled: bool = True  # Preserve legacy ROS leader mode switching.
    port: str = ""
    baudrate: int = Field(default=1000000, ge=9600, le=3000000)
    motor_ids: list[int] = Field(default_factory=lambda: list(range(1, 8)))
    directions: list[float] = Field(default_factory=lambda: [1.0] * 7)
    offsets: list[float] = Field(default_factory=lambda: [0.0] * 7)
    gripper_scale: float = Field(default=0.085, gt=0, le=1)
    gripper_offset: float = -0.005
    gripper_range_degrees: float = Field(default=40, gt=0, le=180)


class Leader(Model):
    backend: Literal["sim", "ros", "sii_serial"] = "sim"
    driver: Literal["eaiarm", "arx5"] = "eaiarm"
    ros_domain_id: int = Field(default=0, ge=0, le=232)
    arms: list[LeaderArm] = Field(
        default_factory=lambda: [
            LeaderArm(
                name=n,
                state_topic=f"/{n}/joint_states",
                target_topic=f"/{n}/joint_targets",
                mode_topic=f"/master/set_control_mode_{n}",
                port=f"/dev/ttyACM{i + 2}",
            )
            for i, n in enumerate(("left", "right"))
        ]
    )
    feedback_hz: int = Field(default=30, ge=10, le=100)
    passive_supported: bool = False
    serial_support_validated: bool = False
    calibration_file: str = "state/leader-calibration.json"
    homing: Homing = Field(default_factory=Homing)

    @model_validator(mode="after")
    def arm_contract(self):
        if [arm.name for arm in self.arms] != ["left", "right"]:
            raise ValueError("arms must be ordered left, right")
        for arm in self.arms:
            if any(len(values) != 7 for values in (arm.motor_ids, arm.directions, arm.offsets)):
                raise ValueError("leader mappings need seven entries")
            if len(set(arm.motor_ids)) != 7 or any(d not in (-1, 1) for d in arm.directions):
                raise ValueError("motor IDs must be unique; directions must be -1 or 1")
            if self.backend == "ros":
                arm.validate_ros(arm.target_topic)
                if (
                    arm.mode_switch_enabled
                    and arm.mode_confirmation == "openeai_graph"
                    and self.driver != "eaiarm"
                ):
                    raise ValueError("OpenEAI graph confirmation requires the eaiarm driver")
        return self


class RobotProfile(Model):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    config: Robot

    @model_validator(mode="after")
    def hardware_backend(self):
        if self.config.backend != "ros":
            raise ValueError("saved follower profiles require ROS control")
        return self


class LeaderProfile(Model):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    config: Leader

    @model_validator(mode="after")
    def hardware_backend(self):
        if self.config.backend == "sim":
            raise ValueError("saved leader profiles require ROS or 3D printed serial control")
        return self


BUILTIN_ARM_PROFILE_IDS = {
    "robot": ("eaiarm-ros", "arx5-ros"),
    "leader": ("eaiarm-ros", "printed-serial"),
}


class ArmProfiles(Model):
    """Saved templates are inert until copied into the effective arm config."""

    robot: list[RobotProfile] = Field(default_factory=list, max_length=32)
    leader: list[LeaderProfile] = Field(default_factory=list, max_length=32)
    selected_robot: str = Field(default="", max_length=64)
    selected_leader: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def profile_ids(self):
        for role in ("robot", "leader"):
            profiles = getattr(self, role)
            ids = [profile.id for profile in profiles]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{role} profile IDs must be unique")
            if set(ids) & set(BUILTIN_ARM_PROFILE_IDS[role]):
                raise ValueError(f"{role} custom profile IDs cannot replace built-in profiles")
            if any(not profile.name.strip() for profile in profiles):
                raise ValueError("profile names cannot be blank")
            selected = getattr(self, f"selected_{role}")
            if selected and selected not in {*ids, *BUILTIN_ARM_PROFILE_IDS[role]}:
                raise ValueError(f"selected {role} profile does not exist")
        return self


class Camera(Model):
    name: Literal["main", "left_hand", "right_hand"]
    backend: Literal["usb", "ros", "sim"] = "sim"
    device: str = ""
    topic: str = ""
    width: int = Field(default=640, ge=64, le=1920)
    height: int = Field(default=480, ge=64, le=1080)
    fps: int = Field(default=60, ge=1, le=120)
    rotation: Literal[0, 90, 180, 270] = 180
    fourcc: Literal["MJPG", "YUYV"] = "MJPG"
    buffer_size: int = Field(default=1, ge=1, le=4)
    warmup_frames: int = Field(default=10, ge=0, le=120)
    configure_v4l2: bool = True

    @property
    def output_shape(self) -> tuple[int, int, int]:
        return (self.width, self.height, 3) if self.rotation in (90, 270) else (self.height, self.width, 3)


class Policy(Model):
    runner_location: Literal["robot", "client", "cloud"] = "robot"
    backend: Literal["openpi", "sim"] = "sim"
    url: str = "wss://policy.example.invalid"
    ca_file: str = ""  # Empty uses system trust; independent from the gateway CA.
    policy_id: str = Field(default="shirt-policy-v1", min_length=1, max_length=128)
    api_key_env: str = "TELEOP_POLICY_API_KEY"
    request_format: Literal["pi05", "openpi", "lerobot"] = "pi05"
    action_hz: int = Field(default=30, ge=1, le=100)
    request_timeout_seconds: float = Field(default=2, ge=0.1, le=60)
    inference_wait_ms: int = Field(
        default=500, ge=50, le=2000,
        description="正常推理等待预算；另加现有命令传输余量，等待时保持最后位置。",
    )
    max_queued_actions: int = Field(default=49, ge=0, le=100)
    max_chunk_age_ms: int = Field(default=1000, ge=50, le=2000)
    @model_validator(mode="before")
    @classmethod
    def discard_legacy_source_path(cls, values):
        # Existing deployments may still contain the removed external source path.
        if isinstance(values, dict) and "lerobot_source" in values:
            values = dict(values)
            values.pop("lerobot_source")
        return values


class Recording(Model):
    root: str = "datasets"
    inspection_roots: list[str] = Field(default_factory=list)
    repo_id: str = "local/remote_teleop"
    fps: int = Field(default=30, ge=1, le=120)
    codec: Literal["h264", "libx264", "hevc"] = "h264"
    queue_size: int = Field(default=180, ge=10, le=600)
    minimum_free_gb: float = Field(default=5, ge=0.01, le=1000)
    max_episode_seconds: int = Field(default=300, ge=1, le=3600)
    max_sync_skew_ms: int = Field(default=50, ge=5, le=250)
    require_recording_for_hil: bool = True
    separate_episode_files: Literal[True] = True


class UI(Model):
    host: str = "127.0.0.1"
    port: int = Field(default=8810, ge=1024, le=65535)
    language: Literal["zh", "en"] = "zh"
    jpeg_quality: int = Field(default=80, ge=40, le=95)
    display_fps: int = Field(default=20, ge=5, le=30)
    intervention_key: str = Field(default="i", pattern=r"^[a-z]$")
    success_key: str = Field(default="s", pattern=r"^[a-z]$")
    failure_key: str = Field(default="f", pattern=r"^[a-z]$")
    discard_key: str = Field(default="a", pattern=r"^[a-z]$")


class AppConfig(Model):
    schema_version: Literal[1] = 1
    general: General = Field(default_factory=General)
    network: Network = Field(default_factory=Network)
    security: Security = Field(default_factory=Security)
    safety: Safety = Field(default_factory=Safety)
    robot: Robot = Field(default_factory=Robot)
    leader: Leader = Field(default_factory=Leader)
    arm_profiles: ArmProfiles = Field(default_factory=ArmProfiles)
    cameras: list[Camera] = Field(
        default_factory=lambda: [Camera(name=n) for n in ("main", "left_hand", "right_hand")]
    )
    policy: Policy = Field(default_factory=Policy)
    recording: Recording = Field(default_factory=Recording)
    ui: UI = Field(default_factory=UI)

    @model_validator(mode="before")
    @classmethod
    def legacy_leader_interfaces(cls, values):
        # Older deployments shared the follower ROS domain implicitly. Preserve
        # it and same-side joint names, while honoring explicit leader settings.
        if isinstance(values, dict) and isinstance(values.get("leader", {}), dict):
            values = deepcopy(values)
            leader = values.setdefault("leader", {})
            robot = values.get("robot", {})
            if "ros_domain_id" not in leader:
                domain = (
                    robot.get("ros_domain_id", 0)
                    if isinstance(robot, dict)
                    else getattr(robot, "ros_domain_id", 0)
                )
                leader["ros_domain_id"] = domain
            robot_arms = robot.get("arms", []) if isinstance(robot, dict) else getattr(robot, "arms", [])
            names = {}
            if isinstance(robot_arms, (list, tuple)):
                for arm in robot_arms:
                    if isinstance(arm, dict) and "joint_names" in arm:
                        names[arm.get("name")] = arm["joint_names"]
                    elif isinstance(arm, Arm):
                        names[arm.name] = arm.joint_names
            if names:
                leader_arms = leader.setdefault(
                    "arms", [arm.model_dump(exclude={"joint_names"}) for arm in Leader().arms]
                )
                if isinstance(leader_arms, list):
                    for arm in leader_arms:
                        if isinstance(arm, dict) and "joint_names" not in arm and arm.get("name") in names:
                            arm["joint_names"] = deepcopy(names[arm["name"]])
        return values

    @model_validator(mode="after")
    def consistency(self):
        if sorted(c.name for c in self.cameras) != ["left_hand", "main", "right_hand"]:
            raise ValueError("exactly one main, left_hand and right_hand camera required")
        s = self.safety
        if s.tracking_error_hard_limit < s.tracking_error_limit:
            raise ValueError("tracking hard limit must be at least the sustained tracking limit")
        trial = s.attended_test
        if trial.gripper_commands is not None and len(trial.gripper_commands) != 2:
            raise ValueError("fixed gripper commands must contain left and right values, or be null")
        if trial.enabled and (
            self.general.operation_mode != "teleop"
            or s.hardware_validated
            or self.robot.stop_strategy != "position_hold"
            or self.leader.backend not in ("ros", "sim")
            or trial.expires_at is not None
            and trial.expires_at <= 0
        ):
            raise ValueError("attended mode requires unvalidated teleop and position hold")
        if any(len(v) != 7 for v in (s.joint_min, s.joint_max, s.velocity_max, s.acceleration_max)):
            raise ValueError("safety limits need seven entries, repeated per arm")
        if any(lo >= hi for lo, hi in zip(s.joint_min, s.joint_max)):
            raise ValueError("joint_min must be below joint_max")
        if trial.gripper_commands is not None and any(
            v < s.joint_min[6] or v > s.joint_max[6] for v in trial.gripper_commands
        ):
            raise ValueError("fixed gripper commands must be within the configured gripper limits")
        if any(v <= 0 for v in s.velocity_max + s.acceleration_max):
            raise ValueError("velocity and acceleration limits must be positive")
        if s.checks.control_loop_watchdog and s.watchdog_check_ms >= s.watchdog_ms / 2:
            raise ValueError("watchdog check period must be less than half the timeout")
        if self.recording.fps > s.control_hz:
            raise ValueError("recording fps cannot exceed control rate")
        if (
            len({self.ui.intervention_key, self.ui.success_key, self.ui.failure_key, self.ui.discard_key})
            != 4
        ):
            raise ValueError("keyboard shortcuts must be distinct")
        parsed = urlparse(self.network.gateway_url)
        if (
            parsed.scheme not in ("ws", "wss")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("gateway_url must be ws(s), without credentials or query")
        policy_url = urlparse(self.policy.url)
        if (
            policy_url.scheme not in ("ws", "wss", "http", "https")
            or not policy_url.hostname
            or policy_url.username
            or policy_url.password
            or policy_url.query
            or policy_url.fragment
        ):
            raise ValueError("policy URL must not contain credentials; use api_key_env")
        if parsed.scheme == "ws" and (
            not self.security.allow_loopback_http or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError("plaintext transport is allowed only on loopback")
        if self.general.simulation:
            if (
                self.robot.backend != "sim"
                or self.leader.backend != "sim"
                or any(c.backend != "sim" for c in self.cameras)
            ):
                raise ValueError("simulation must use simulated robot, leader and cameras")
        else:
            if (
                self.robot.backend != "ros"
                or self.leader.backend == "sim"
                or self.policy.backend == "sim"
                or any(c.backend == "sim" for c in self.cameras)
            ):
                raise ValueError("hardware deployment cannot contain simulated adapters")
            # Real sensors may run before motion commissioning. The controller still
            # refuses arming until hardware_validated is explicitly set and restarted.
            if self.robot.stop_strategy == "driver_ack" and any(
                not a.stop_topic or not a.stop_state_topic for a in self.robot.arms
            ):
                raise ValueError("hardware requires driver stop command and acknowledgement topics")
            if self.leader.backend == "ros" and any(
                a.mode_switch_enabled and a.mode_confirmation == "topic" and not a.mode_state_topic.strip()
                for a in self.leader.arms
            ):
                raise ValueError("ROS leader requires mode acknowledgement topics")
            if self.robot.backend == "ros" and any(
                a.mode_switch_enabled and a.mode_confirmation == "topic" and not a.mode_state_topic.strip()
                for a in self.robot.arms
            ):
                raise ValueError("ROS follower requires mode acknowledgement topics")
            if self.leader.backend == "sii_serial" and not (
                self.leader.passive_supported and self.leader.serial_support_validated
            ):
                raise ValueError("serial leader requires validated mechanical support and passive mode")
            if parsed.scheme != "wss" or urlparse(self.policy.url).scheme not in ("https", "wss"):
                raise ValueError("hardware requires verified TLS for gateway and policy")
        return self

    @property
    def control_contract(self) -> str:
        """Reject mixed deployments before a peer can become a controller."""
        data = {
            "version": 3,
            "robot_id": self.general.robot_id,
            "simulation": self.general.simulation,
            "mode": self.general.operation_mode,
            "joints": self.joint_names,
            "runner": self.policy.runner_location,
            "policy_id": self.policy.policy_id,
            "policy_backend": self.policy.backend,
            "fps": self.recording.fps,
            "cameras": [(c.name, c.width, c.height, c.rotation) for c in self.cameras],
            "limits": [self.safety.joint_min, self.safety.joint_max],
            "hardware_validated": self.safety.hardware_validated,
            "attended_test": self.safety.attended_test.model_dump(),
            "safety": self.safety.model_dump(),
            "stop_strategy": self.robot.stop_strategy,
            "robot": self.robot.model_dump(),
            "leader": self.leader.model_dump(),
            "gripper_feedback": [
                (a.gripper_feedback_scale, a.gripper_feedback_offset) for a in self.robot.arms
            ],
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @property
    def connection_contract(self) -> str:
        # Safety can be reconciled over an authenticated connection after a
        # partial update/restart. Motion still requires the full live contract.
        return self.model_copy(update={"safety": Safety()}).control_contract

    @property
    def joint_names(self) -> list[str]:
        return [
            f"{side}_{joint}.pos"
            for side in ("left", "right")
            for joint in [*[f"joint_{i}" for i in range(1, 7)], "gripper"]
        ]


class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.current = AppConfig.model_validate_json(self.path.read_text())
        self.active_revision = self.revision
        self.lock = threading.RLock()
        self.defaults_path = self.path.parent / "defaults" / self.path.name
        # Capture this deployment once, including its hardware mappings. Ordinary
        # saves and process restarts must never replace the recovery baseline.
        with self.path.with_suffix(".lock").open("a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            if not self.defaults_path.exists():
                atomic_json(self.defaults_path, self.current.model_dump())

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def read(self):
        raw = self.path.read_bytes()
        revision = hashlib.sha256(raw).hexdigest()
        saved = AppConfig.model_validate_json(raw)
        return {
            # Older files omit newly added fields. The form needs the same
            # validated defaults as the running process, without rewriting disk.
            "config": saved.model_dump(),
            "revision": revision,
            "restart_required": saved.model_dump(exclude={"arm_profiles"})
            != self.current.model_dump(exclude={"arm_profiles"}),
            "active_config": self.current.model_dump(),
            "active_contract": self.current.control_contract,
            "hot_update_pending": saved.safety != self.current.safety,
            "defaults_created_at": self.defaults_path.stat().st_mtime,
        }

    def read_defaults(self):
        return {
            "config": AppConfig.model_validate_json(self.defaults_path.read_bytes()).model_dump(),
            "created_at": self.defaults_path.stat().st_mtime,
        }

    def apply_safety(self, revision):
        with self.lock, self.path.with_suffix(".lock").open("a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            if revision != self.revision:
                raise ValueError("CONFIG_CONFLICT")
            saved = AppConfig.model_validate_json(self.path.read_bytes())
            candidate = self.current.model_dump()
            candidate["safety"] = saved.safety.model_dump()
            candidate["arm_profiles"] = saved.arm_profiles.model_dump()
            validated = AppConfig.model_validate(candidate)
            # All adapters retain this root object. Swap one complete validated
            # safety snapshot, rather than changing fields one at a time.
            self.current.safety = validated.safety
            self.current.arm_profiles = validated.arm_profiles
            return self.read()

    def save(self, data: dict, revision: str):
        with self.lock, self.path.with_suffix(".lock").open("a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            try:
                if revision != self.revision:
                    raise ValueError("CONFIG_CONFLICT")
                cfg = AppConfig.model_validate(data)
                atomic_json(self.path, cfg.model_dump())
                return self.read()
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
