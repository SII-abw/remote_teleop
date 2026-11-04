"""Migrate a deployment's arm schema while preserving its effective interfaces.

Dry run by default. Never connects to ROS, serial devices, or running services.
Back up deployment files before using --write.
"""

import argparse
import copy
import json
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json


def normalized(raw):
    config = AppConfig.model_validate(raw).model_dump()
    library = config["arm_profiles"]
    for role in ("robot", "leader"):
        if config[role]["backend"] == "sim":
            continue
        if library[f"selected_{role}"]:
            continue
        effective = copy.deepcopy(config[role])
        identity = f"site-{role}-{effective['backend']}"
        if any(profile["id"] == identity for profile in library[role]):
            continue
        library[role].append(
            {
                "id": identity,
                "name": "现场从臂配置" if role == "robot" else "现场遥操臂配置",
                "config": effective,
            }
        )
        library[f"selected_{role}"] = identity
    return AppConfig.model_validate(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument("--write", action="store_true", help="write validated normalized configuration")
    arguments = parser.parse_args()
    # Validate every candidate before writing any file.
    candidates = [(path, normalized(json.loads(path.read_text()))) for path in arguments.config]
    for path, config in candidates:
        if arguments.write:
            atomic_json(path, config.model_dump())
        print(json.dumps({"path": str(path), "valid": True, "written": arguments.write}))


if __name__ == "__main__":
    main()
