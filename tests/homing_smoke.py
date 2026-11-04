"""Three-process zero-return acceptance using simulation only."""

import json
import time

from integration_smoke import ROOT, Stack
from remote_teleop.config import atomic_json


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    stack.config.robot.homing.duration_seconds = 1
    stack.config.leader.homing.duration_seconds = 1
    stack.config.safety.operator_lease_seconds = 0.6
    for path in stack.configs.values():
        atomic_json(path, stack.config.model_dump())
    try:
        stack.start()
        stack.api("/api/simulation/leader", {"q": [0.06] * 6 + [0.01] + [0.06] * 6 + [0.01]})
        time.sleep(0.25)
        job = stack.control("home", {"target": "leader"})
        stack.wait(
            lambda s: (s.get("homing") or {}).get("id") == job["id"]
            and s["homing"]["state"] in ("COMPLETE", "FAILED"),
            "leader home",
        )
        assert stack.status["homing"]["state"] == "COMPLETE", stack.status
        assert stack.status["mode"] == "DISARMED", stack.status
        assert max(abs(q) for q in stack.status["leader_q"]) < 0.01, stack.status
        stack.control("arm")
        stack.wait(lambda s: s.get("mode") == "READY", "armed")
        try:
            stack.control("home", {"target": "robot"})
        except ValueError as exc:
            assert "DISARM_REQUIRED" in str(exc), exc
        else:
            raise AssertionError("Homing accepted while armed")
        stack.control("disarm")
        stack.wait(lambda s: s.get("mode") == "DISARMED", "disarmed")
        job = stack.control("home", {"target": "robot"})
        stack.wait(
            lambda s: (s.get("homing") or {}).get("id") == job["id"] and s.get("mode") == "HOMING",
            "follower home started",
        )
        stack.control("home_cancel")
        stack.wait(
            lambda s: s.get("mode") == "DISARMED" and s["homing"]["state"] == "CANCELLED", "homing cancelled"
        )
        job = stack.control("home", {"target": "robot"})
        stack.wait(
            lambda s: (s.get("homing") or {}).get("id") == job["id"]
            and s["homing"]["state"] in ("COMPLETE", "FAILED"),
            "follower home",
        )
        assert stack.status["homing"]["state"] == "COMPLETE", stack.status
        assert stack.status["mode"] == "DISARMED", stack.status
        job = stack.control("home", {"target": "robot"})
        stack.wait(
            lambda s: (s.get("homing") or {}).get("id") == job["id"] and s.get("mode") == "HOMING",
            "estop test started",
        )
        current = stack.api("/api/config/client")
        try:
            stack.api("/api/config/client", current)
        except ValueError as exc:
            assert "DISARM_REQUIRED" in str(exc), exc
        else:
            raise AssertionError("Configuration changed during homing")
        stack.control("estop")
        stack.wait(
            lambda s: s.get("mode") == "ESTOP_LATCHED" and s["homing"]["state"] == "FAILED", "homing estop"
        )
        stack.control("reset")
        stack.wait(lambda s: s.get("mode") == "DISARMED", "reset after homing estop")
        job = stack.control("home", {"target": "robot"})
        stack.wait(
            lambda s: (s.get("homing") or {}).get("id") == job["id"] and s.get("mode") == "HOMING",
            "lease test started",
        )
        stack.poll_stop.set()
        stack.poller.join(timeout=2)
        time.sleep(1.2)
        final = stack.api("/api/status")
        assert final["mode"] == "SAFE_HOLD", final
        assert final["homing"]["state"] == "FAILED", final
        assert final["homing"]["error"] == "HOMING_OPERATOR_REQUIRED", final
        report = {
            "passed": True,
            "hardware_opened": False,
            "leader_and_follower_zero": True,
            "armed_home_rejected": True,
            "cancel_returns_disarmed": True,
            "operator_loss_stops": True,
            "estop_stops_homing": True,
            "config_save_during_home_rejected": True,
            "directory": str(stack.path),
        }
        atomic_json(ROOT / "state/acceptance-homing.json", report)
        print(json.dumps(report), flush=True)
    finally:
        stack.close()


if __name__ == "__main__":
    main()
