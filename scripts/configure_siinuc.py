"""Create this site's real ROS deployment without enabling motion or changing legacy files.

Run on siinuc with an installed remote_teleop environment. Existing configuration and
credentials are never overwritten. TLS keys and credentials stay under state/.
"""

import json
import subprocess
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json
from remote_teleop.security import create_credentials


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-url", required=True, help="WSS policy endpoint")
    parser.add_argument("--inspection-root", action="append", default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_dir = root / "configs/siinuc"
    config_dir.mkdir(parents=True, exist_ok=True)
    state = root / "state/siinuc"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = state / "gateway.pem", state / "gateway.key"
    if not cert.exists() and not key.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-sha256",
                "-nodes",
                "-days",
                "90",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=siinuc-remote-teleop",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:siinuc,DNS:siinuc2",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        key.chmod(0o600)
    if not cert.is_file() or not key.is_file():
        raise SystemExit("Incomplete existing TLS files; inspect them before proceeding")

    cfg = AppConfig()
    cfg.general.robot_id = "siinuc-dual-arx"
    cfg.general.simulation = False
    cfg.general.operation_mode = "teleop"
    cfg.robot.backend, cfg.robot.stop_strategy = "ros", "position_hold"
    cfg.leader.backend = "ros"
    for arm in cfg.leader.arms:
        arm.mode_confirmation = "openeai_graph"
    cfg.safety.hardware_validated = False
    cfg.safety.velocity_max = [0.25] * 6 + [0.03]
    cfg.safety.acceleration_max = [0.5] * 6 + [0.1]
    cfg.safety.leader_lease_ms = 1000
    cfg.network.gateway_url = "wss://127.0.0.1:8800"
    cfg.network.ca_file = str(cert)
    cfg.network.certificate_file, cfg.network.private_key_file = str(cert), str(key)
    cfg.security.credentials_file = str(state / "credentials.json")
    cfg.policy.backend = "openpi"
    cfg.policy.url = args.policy_url
    cfg.recording.root = str(root / "datasets/siinuc")
    cfg.recording.inspection_roots = args.inspection_root
    # Follow the HIL reference's named camera mapping, which differs from the
    # older camera YAML. Confirm physical labels visually during commissioning.
    ports = {"main": 6, "left_hand": 4, "right_hand": 5}
    for camera in cfg.cameras:
        camera.backend = "usb"
        camera.device = f"/dev/v4l/by-path/pci-0000:00:14.0-usb-0:{ports[camera.name]}:1.0-video-index0"
        if not Path(camera.device).exists():
            raise SystemExit(f"Camera missing: {camera.name} ({camera.device})")
    cfg = AppConfig.model_validate(cfg.model_dump())
    if not Path(cfg.security.credentials_file).exists():
        create_credentials(cfg.security.credentials_file, cfg.general.robot_id, hours=168)
    credentials = json.loads(Path(cfg.security.credentials_file).read_text())["credentials"]
    token_file = state / "operator-token.txt"
    if not token_file.exists():
        with token_file.open("x") as output:
            token_file.chmod(0o600)
            output.write(next(e["token"] for e in credentials if e["role"] == "ui") + "\n")
    for role in ("cloud", "slave", "master"):
        role_cfg = cfg.model_copy(deep=True)
        if role == "master":
            # Only the local UI uses HTTP; access from other computers via SSH.
            # Its cloud connection still verifies the WSS certificate above.
            role_cfg.network.certificate_file = ""
            role_cfg.network.private_key_file = ""
        path = config_dir / f"{role}.json"
        if not path.exists():
            atomic_json(path, role_cfg.model_dump())
        AppConfig.model_validate_json(path.read_text())
    (root / "datasets/siinuc").mkdir(parents=True, exist_ok=True)
    print(f"Real ROS configurations: {config_dir}; motion locked pending onsite commissioning")
    print(f"Operator token file (0600): {token_file}")


if __name__ == "__main__":
    main()
