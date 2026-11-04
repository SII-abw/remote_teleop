"""Three-process profile acceptance on loopback simulation; never opens hardware."""

import copy
import json
import time

from integration_smoke import ROOT, Stack
from remote_teleop.config import atomic_json


def main():
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", "teleop")
    try:
        stack.start()
        catalog = stack.api("/api/arm-profiles")
        assert {p["config"]["driver"] for p in catalog["robot"]} == {"eaiarm", "arx5"}
        assert {p["config"]["backend"] for p in catalog["leader"]} == {"ros", "sii_serial"}
        current = stack.api("/api/config/client")
        original_contract = current["active_contract"]
        profiles = current["config"]["arm_profiles"]
        for role in ("robot", "leader"):
            for index, template in enumerate(catalog[role]):
                custom = copy.deepcopy(template)
                custom["id"] = f"custom-{role}-{index}"
                custom["name"] = f"现场 {role} 自定义 {index}"
                profiles[role].append(custom)
        saved = stack.api("/api/config/client", current)
        assert not saved["restart_required"], saved
        assert saved["restart_targets"] == [], saved
        for role in ("robot", "client", "cloud"):
            peer = stack.api("/api/config/" + role)
            assert peer["config"]["arm_profiles"] == profiles
            assert peer["active_contract"] == original_contract
            assert not peer["restart_required"], peer
        # A target/topic mapping change is an effective interface change and is
        # persisted on all roles, but never applied to running adapters in place.
        current = stack.api("/api/config/client")
        current["config"]["leader"]["arms"][0]["target_topic"] = "/profile_test/left/target"
        saved = stack.api("/api/config/client", current)
        assert set(saved["restart_targets"]) == {"robot", "client", "cloud"}, saved
        for role in ("robot", "client", "cloud"):
            peer = stack.api("/api/config/" + role)
            assert peer["config"]["leader"]["arms"][0]["target_topic"] == "/profile_test/left/target"
            assert peer["active_config"]["leader"]["arms"][0]["target_topic"] != "/profile_test/left/target"
            assert peer["active_contract"] == original_contract
        # Restore the effective simulation interface, retaining saved profiles.
        current = stack.api("/api/config/client")
        current["config"]["leader"] = copy.deepcopy(current["active_config"]["leader"])
        saved = stack.api("/api/config/client", current)
        assert not saved["restart_targets"], saved
        stack.control("arm")
        stack.wait(lambda s: s.get("mode") == "READY", "simulated arming")
        current = stack.api("/api/config/client")
        try:
            stack.api("/api/config/client", current)
        except ValueError as exc:
            assert "DISARM_REQUIRED" in str(exc), exc
        else:
            raise AssertionError("Profile save was accepted while armed")
        stack.control("disarm")
        stack.wait(lambda s: s.get("mode") == "DISARMED", "simulated disarming")
        # The catalog remains immutable even after both custom libraries change.
        assert stack.api("/api/arm-profiles") == catalog
        report = {
            "passed": True,
            "time": time.time(),
            "custom_profiles_per_role": 2,
            "three_role_persistence": True,
            "profile_library_requires_restart": False,
            "effective_interface_requires_restart": True,
            "armed_save_rejected": True,
            "hardware_opened": False,
            "log_directory": str(stack.path),
        }
        atomic_json(ROOT / "state" / "acceptance-arm-profiles.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        stack.close()


if __name__ == "__main__":
    main()
