"""Three simulated processes only: joint-only testing and active video shutdown."""

import json
import signal
import time

from integration_smoke import Stack
from remote_teleop.config import atomic_json


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    cfg = stack.config
    cfg.robot.stop_strategy = "position_hold"
    trial = cfg.safety.attended_test
    trial.enabled, trial.expires_at = True, time.time() + 300
    trial.gripper_commands = [0.002, 0]
    for path in stack.configs.values():
        atomic_json(path, cfg.model_dump())
    streams = []
    try:
        stack.start()
        # Large initial pose mismatch must not create a catch-up movement.
        leader = [0.3] * 14
        stack.api("/api/simulation/leader", {"q": leader})
        stack.wait(lambda s: s.get("leader_q") == leader, "initial leader pose")
        stack.control("arm")
        stack.wait(lambda s: s.get("mode") == "READY", "armed")
        stack.control("start")
        stack.wait(lambda s: s.get("mode") == "HUMAN_ACTIVE", "relative takeover")
        assert max(abs(v) for i, v in enumerate(stack.status["q"]) if i % 7 != 6) < 0.001
        leader[0] += 0.02
        leader[6] = 0.8
        stack.api("/api/simulation/leader", {"q": leader})
        stack.wait(lambda s: abs(s["q"][0] - 0.02) < 0.002, "bounded relative movement")
        assert stack.status["q"][6] == 0.002 and stack.status["q"][13] == 0
        leader[0] = 0.36
        stack.api("/api/simulation/leader", {"q": leader})
        stack.wait(lambda s: s.get("mode") == "SAFE_HOLD", "range stop")
        assert stack.status["reason"] == "ATTENDED_TEST_RANGE", stack.status
        stack.control("reset")
        stack.wait(lambda s: s.get("mode") == "DISARMED", "reset")
        try:
            stack.control("arm")
            raise AssertionError("Used test rearmed")
        except ValueError as exc:
            assert "ATTENDED_TEST_UNAVAILABLE" in str(exc)
        for name in ("main", "left_hand", "right_hand"):
            response = stack.opener.open(stack.base + "/api/video/" + name, timeout=5)
            assert response.read(7) == b"--frame"
            streams.append(response)
        started = time.monotonic()
        stack.processes["master"].send_signal(signal.SIGTERM)
        stack.processes["master"].wait(timeout=5)
        elapsed = time.monotonic() - started
        # Recent Uvicorn versions re-raise SIGTERM after a graceful shutdown.
        assert stack.processes["master"].returncode in (0, -signal.SIGTERM)
        log = (stack.path / "master.log").read_text()
        assert "timeout graceful shutdown exceeded" not in log and "Exception in ASGI" not in log
        result = {"passed": True, "shutdown_seconds": elapsed, "path": str(stack.path)}
        atomic_json(stack.path / "result.json", result)
        print(json.dumps(result))
    finally:
        for stream in streams:
            stream.close()
        stack.close()


if __name__ == "__main__":
    main()
