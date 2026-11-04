"""Mixed live safety after an interrupted update: block motion, repair online."""

import json

from integration_smoke import Stack
from remote_teleop.config import atomic_json


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    different = stack.config.model_dump()
    different["safety"]["velocity_max"] = [0.25] * 6 + [0.04]
    atomic_json(stack.configs["slave"], different)
    try:
        stack.start()
        stack.wait(lambda status: status.get("config_in_sync") is False, "mixed live configuration")
        try:
            stack.control("arm")
            raise AssertionError("Motion accepted with different live limits")
        except ValueError as error:
            assert "CONFIG_NOT_SYNCHRONIZED" in str(error), error
        pids = {role: process.pid for role, process in stack.processes.items()}
        current = stack.api("/api/config/client")
        result = stack.api("/api/config/client", current)
        assert result["hot_applied"] and not result["restart_targets"]
        stack.wait(lambda status: status.get("config_in_sync"), "live repair")
        stack.control("arm")
        stack.wait(lambda status: status.get("mode") == "READY", "arm after live repair")
        stack.control("disarm")
        stack.wait(lambda status: status.get("mode") == "DISARMED", "disarm")
        assert pids == {role: process.pid for role, process in stack.processes.items()}
        print(
            json.dumps(
                {
                    "passed": True,
                    "mismatch_blocks_arm": True,
                    "repaired_without_restart": True,
                    "path": str(stack.path),
                }
            )
        )
    finally:
        stack.close()


if __name__ == "__main__":
    main()
