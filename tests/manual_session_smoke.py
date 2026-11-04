"""Simulation only: free grippers, full manual motion, rearming and config API."""

import json

from integration_smoke import Stack
from remote_teleop.config import atomic_json
from test_safety_configuration import manual_config


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    cfg = stack.config
    manual = manual_config()
    cfg.safety = manual.safety
    cfg.robot.stop_strategy = manual.robot.stop_strategy
    for path in stack.configs.values():
        atomic_json(path, cfg.model_dump())
    try:
        stack.start()
        schema = stack.api("/api/config/schema")
        assert "checks" in schema["$defs"]["Safety"]["properties"]
        trial_schema = schema["$defs"]["AttendedTest"]["properties"]
        assert {t["type"] for t in trial_schema["gripper_commands"]["anyOf"]} == {"array", "null"}
        for target in ("client", "robot", "cloud"):
            result = stack.api("/api/config/" + target)
            assert result["config"]["safety"]["attended_test"]["duration_seconds"] is None
        for turn in range(2):
            leader = [0.0] * 14
            leader[0] = 0.3
            stack.api("/api/simulation/leader", {"q": leader})
            stack.wait(lambda s: s.get("leader_q") == leader, "leader initial pose")
            stack.control("arm")
            stack.wait(lambda s: s.get("mode") == "READY", "armed")
            stack.control("start")
            stack.wait(lambda s: s.get("mode") == "HUMAN_ACTIVE", "manual active")
            initial = stack.status["q"].copy()
            leader[0] += 0.25
            leader[7] += 0.15
            leader[6], leader[13] = 0.04, 0.03
            stack.api("/api/simulation/leader", {"q": leader})
            stack.wait(
                lambda s: (
                    s.get("mode") == "HUMAN_ACTIVE"
                    and abs(s["q"][0] - initial[0] - 0.25) < 0.01
                    and abs(s["q"][6] - 0.04) < 0.003
                    and abs(s["q"][13] - 0.03) < 0.003
                ),
                "full motion and free grippers",
            )
            assert stack.status["recording_permitted"]
            stack.control("pause")
            stack.wait(lambda s: s.get("mode") == "SAFE_HOLD", "pause")
            stack.control("reset")
            stack.wait(
                lambda s: s.get("mode") == "DISARMED" and s.get("motion_permitted"), "repeat availability"
            )
        # Existing acceptance flow exercises actual dataset recording as well.
        stack.exercise()
        print(json.dumps({"passed": True, "repeat_sessions": 2, "recording": True, "path": str(stack.path)}))
    finally:
        stack.close()


if __name__ == "__main__":
    main()
