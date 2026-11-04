"""Three simulated roles: restore deployment defaults through authenticated APIs."""

import json
import urllib.error
import urllib.request

from integration_smoke import Stack


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    try:
        stack.start()
        try:
            urllib.request.urlopen(stack.base + "/api/config/defaults/client")
            raise AssertionError("Unauthenticated defaults access accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        originals = {}
        for target in ("client", "robot", "cloud"):
            original = stack.api("/api/config/" + target)
            baseline = stack.api("/api/config/defaults/" + target)
            assert original["config"] == baseline["config"]
            assert baseline["created_at"] == original["defaults_created_at"]
            edited = json.loads(json.dumps(original["config"]))
            edited["ui"]["language"] = "en"
            saved = stack.api("/api/config/" + target, {"config": edited, "revision": original["revision"]})
            assert saved["restart_required"]
            assert stack.api("/api/config/defaults/" + target) == baseline
            restored = stack.api(
                "/api/config/" + target, {"config": baseline["config"], "revision": saved["revision"]}
            )
            assert restored["config"] == baseline["config"]
            assert not restored["restart_required"]
            originals[target] = restored
        # Apply speed, acceleration and checks without replacing any process.
        pids = {role: process.pid for role, process in stack.processes.items()}
        shared = json.loads(json.dumps(originals["client"]["config"]))
        shared["safety"]["velocity_max"] = [1.0] * 6 + [0.16]
        shared["safety"]["acceleration_max"] = [2.0] * 6 + [0.4]
        shared["safety"]["checks"]["tracking_error"] = False
        shared["safety"]["stationary_window_seconds"] = 0.4
        saved = stack.api(
            "/api/config/client", {"config": shared, "revision": originals["client"]["revision"]}
        )
        assert saved["hot_applied"] and not saved["restart_targets"]
        for target in originals:
            current = stack.api("/api/config/" + target)
            assert current["config"]["safety"] == shared["safety"]
            assert current["active_config"]["safety"] == shared["safety"]
            assert not current["restart_required"]
            assert stack.api("/api/config/defaults/" + target)["config"] == originals[target]["config"]
        stack.wait(lambda status: status.get("config_in_sync") and status.get("healthy"), "live contracts")
        stack.control("arm")
        stack.wait(lambda status: status.get("mode") == "READY", "arm with hot settings")
        stack.control("disarm")
        stack.wait(lambda status: status.get("mode") == "DISARMED", "disarm after hot settings")
        restored = stack.api(
            "/api/config/client",
            {"config": originals["client"]["config"], "revision": saved["revision"]},
        )
        assert not restored["restart_targets"]
        for target in originals:
            current = stack.api("/api/config/" + target)
            assert current["config"] == originals[target]["config"]
            assert not current["restart_required"]
            assert current["active_config"]["safety"] == originals[target]["config"]["safety"]
            originals[target] = current
        assert pids == {role: process.pid for role, process in stack.processes.items()}
        stack.wait(lambda status: status.get("config_in_sync"), "restored live contracts")
        stack.control("arm")
        stack.wait(lambda status: status.get("mode") == "READY", "armed simulation")
        for target, original in originals.items():
            try:
                stack.api(
                    "/api/config/" + target, {"config": original["config"], "revision": original["revision"]}
                )
                raise AssertionError("Restore accepted while armed")
            except ValueError as exc:
                assert "DISARM_REQUIRED" in str(exc), str(exc)
            assert stack.api("/api/config/" + target)["revision"] == original["revision"]
        stack.control("disarm")
        stack.wait(lambda status: status.get("mode") == "DISARMED", "disarmed")
        print(
            json.dumps(
                {
                    "passed": True,
                    "roles": 3,
                    "restore": True,
                    "shared_sync_and_restore": True,
                    "live_apply_without_restart": True,
                    "armed_rejection": True,
                    "path": str(stack.path),
                }
            )
        )
    finally:
        stack.close()


if __name__ == "__main__":
    main()
