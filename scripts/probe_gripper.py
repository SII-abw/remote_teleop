"""One bounded onsite gripper commissioning step; requires attended clearance.

Default is read-only. --execute opens ONE initially closed gripper by a command
of 0.002 over two seconds, keeping all six arm targets at their measured pose.
Requires the new control services stopped and no other target publisher.
Never closes the gripper, homes, resets, or repeats a failed experiment.
"""

import argparse
import json
import signal
import subprocess
import time
from pathlib import Path

from remote_teleop.adapters.hardware import RosIO
from remote_teleop.config import ConfigStore, atomic_json
from remote_teleop.control import Rejected, vector


def check_feedback(baseline, current, side):
    vector(baseline)
    vector(current)
    index = 0 if side == "left" else 7
    for arm_start in (0, 7):
        if max(abs(current[i] - baseline[i]) for i in range(arm_start, arm_start + 6)) > 0.02:
            raise Rejected("ARM_MOVED_DURING_GRIPPER_PROBE")
    if not -0.03 <= current[index + 6] <= 0.25:
        raise Rejected("GRIPPER_READBACK_OUTSIDE_PROBE_BOUND")
    other = 13 if side == "left" else 6
    if abs(current[other] - baseline[other]) > 0.02:
        raise Rejected("OTHER_GRIPPER_MOVED")


def target_at(baseline, side, step):
    vector(baseline)
    if side not in ("left", "right") or not 0 <= step <= 40:
        raise Rejected("INVALID_PROBE_STEP")
    index = 0 if side == "left" else 7
    return baseline[index : index + 6] + [0.002 * step / 40]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=("left", "right"), required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cfg = ConfigStore(root / "configs/siinuc/slave.json").current
    assert not cfg.general.simulation and not cfg.safety.hardware_validated
    if args.execute:
        for role in ("master", "slave", "cloud"):
            state = subprocess.check_output(
                ["systemctl", "--user", "show", f"remote-teleop-{role}", "-p", "ActiveState", "--value"],
                text=True,
            ).strip()
            if state != "inactive":
                raise SystemExit("Stop all new platform services before executing the probe")
    io = RosIO(cfg.robot.ros_domain_id, "remote_teleop_gripper_probe")
    report = {"side": args.side, "execute": args.execute, "samples": [], "outcome": "NOT_STARTED"}
    path = root / "state/siinuc" / f"gripper-{args.side}-{time.time_ns()}.json"

    def interrupted(*_):
        raise InterruptedError("Operator interrupted; no further commands sent")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for arm in cfg.robot.arms:
            io.subscribe_state(arm.name, arm.state_topic, arm.joint_names)

        def read():
            q, stamp = io.read()
            if time.monotonic() - stamp > 0.15:
                raise Rejected("STALE_JOINT_STATE")
            return q

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                baseline = read()
                if io.stationary():
                    break
            except ValueError:
                pass
            time.sleep(0.02)
        else:
            raise Rejected("STATIONARY_FRESH_FEEDBACK_REQUIRED")
        report["baseline"] = baseline
        index = 6 if args.side == "left" else 13
        if abs(baseline[index]) > 0.03:
            raise Rejected("INITIALLY_CLOSED_GRIPPER_REQUIRED")
        print(json.dumps({"side": args.side, "baseline": baseline, "execute": args.execute}), flush=True)
        if not args.execute:
            report["outcome"] = "READ_ONLY"
            return
        arm = next(a for a in cfg.robot.arms if a.name == args.side)
        for other in cfg.robot.arms:
            if io.node.count_publishers(other.command_topic) != 0:
                raise Rejected("COMPETING_TARGET_PUBLISHER")
        publisher = io.node.create_publisher(io.JointState, arm.command_topic, io.command_qos)
        deadline = time.monotonic() + 3
        while publisher.get_subscription_count() != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        if publisher.get_subscription_count() != 1:
            raise Rejected("EXPECTED_SINGLE_ARX_BRIDGE")
        started = time.monotonic()
        for step in range(41):
            current = read()
            check_feedback(baseline, current, args.side)
            if time.monotonic() - started > 3:
                raise Rejected("PROBE_TIMING_OVERRUN")
            if io.node.count_publishers(arm.command_topic) != 1:
                raise Rejected("COMPETING_TARGET_PUBLISHER")
            msg = io.JointState()
            msg.header.stamp = io.node.get_clock().now().to_msg()
            msg.name, msg.position = arm.joint_names, target_at(baseline, args.side, step)
            publisher.publish(msg)
            report["samples"].append(
                {"t": time.monotonic() - started, "target": msg.position.tolist(), "q": current}
            )
            time.sleep(max(0, started + (step + 1) * 0.05 - time.monotonic()))
        # Observe without publishing; the last bounded target remains in the driver.
        for _ in range(20):
            current = read()
            check_feedback(baseline, current, args.side)
            report["samples"].append({"t": time.monotonic() - started, "q": current})
            time.sleep(0.05)
        report["final"] = read()
        report["outcome"] = "COMPLETED_AWAITING_PHYSICAL_OBSERVATION"
        print(
            json.dumps(
                {
                    "outcome": report["outcome"],
                    "gripper_before": baseline[index],
                    "gripper_after": report["final"][index],
                }
            ),
            flush=True,
        )
    except BaseException as exc:
        report["outcome"] = "ABORTED"
        report["error"] = str(exc)
        # Do not issue any new mode/target command after a potential physical stop.
        raise
    finally:
        atomic_json(path, report)
        io.close()
        print(f"Report: {path}", flush=True)


if __name__ == "__main__":
    main()
