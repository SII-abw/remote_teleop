#!/usr/bin/env python3
"""Create matching role configs and scoped credentials, without starting any role."""

import argparse
import json
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json
from remote_teleop.security import create_credentials


ROLE_SCOPES = {"cloud": {"client", "robot"}, "master": {"client", "ui"}, "slave": {"robot"}}


def prepare(template, output, hours=24, gateway_url=None):
    if not 1 <= hours <= 168:
        raise ValueError("hours must be between 1 and 168")
    cfg = AppConfig.model_validate(json.loads(Path(template).read_text()))
    if gateway_url:
        cfg.network.gateway_url = gateway_url
    cfg = AppConfig.model_validate(cfg.model_dump())
    output = Path(output)
    # A deployment bundle contains credentials; fail closed on any existing path.
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    seed = output / "credentials-all.json"
    try:
        create_credentials(seed, cfg.general.robot_id, hours)
        entries = json.loads(seed.read_text())["credentials"]
        for role, scopes in ROLE_SCOPES.items():
            role_cfg = cfg.model_copy(deep=True)
            role_cfg.security.credentials_file = f"state/credentials-{role}.json"
            atomic_json(output / f"{role}.json", role_cfg.model_dump())
            atomic_json(output / f"credentials-{role}.json", {
                "credentials": [entry for entry in entries if entry["role"] in scopes]
            })
    finally:
        seed.unlink(missing_ok=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", default="configs/simulation.json")
    parser.add_argument("--output", default="state/deployment")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--gateway-url")
    args = parser.parse_args()
    path = prepare(args.template, args.output, args.hours, args.gateway_url)
    print(f"Deployment bundle written to {path.resolve()} (contains private credentials).")
    print("Distribute only ROLE.json and credentials-ROLE.json to each host; see deployment guide.")


if __name__ == "__main__":
    main()
