"""Scoped, revocable credentials and TLS contexts. Never log token values."""

import hmac
import json
import os
import secrets
import ssl
import time
from pathlib import Path

from .config import atomic_json


def create_credentials(path, robot_id, hours=24):
    target = Path(path)
    if target.exists():
        raise ValueError("CREDENTIALS_EXIST")
    atomic_json(
        target,
        {
            "credentials": [
                {
                    "role": role,
                    "robot_id": robot_id,
                    "token": secrets.token_urlsafe(48),
                    "expires_at": time.time() + hours * 3600,
                }
                for role in ("client", "robot", "ui")
            ]
        },
    )


class Credentials:
    def __init__(self, cfg):
        self.cfg = cfg

    def entries(self):
        path = Path(self.cfg.security.credentials_file)
        if path.stat().st_mode & 0o077:
            raise ValueError("CREDENTIAL_PERMISSIONS")
        return json.loads(path.read_text())["credentials"]

    def verify(self, token, role, robot_id):
        if not isinstance(token, str) or len(token) < 32:
            return False
        try:
            return any(
                e["role"] == role
                and e["robot_id"] == robot_id
                and e["expires_at"] > time.time()
                and hmac.compare_digest(token, e["token"])
                for e in self.entries()
            )
        except (OSError, ValueError, KeyError):
            return False

    def token(self, role):
        override = os.getenv(self.cfg.security.token_env) if self.cfg.security.token_env else None
        if override:
            return override
        for entry in self.entries():
            if (
                entry["role"] == role
                and entry["robot_id"] == self.cfg.general.robot_id
                and entry["expires_at"] > time.time()
            ):
                return entry["token"]
        raise ValueError("CREDENTIAL_UNAVAILABLE")


def tls_context(cfg):
    if cfg.network.gateway_url.startswith("wss:"):
        return ssl.create_default_context(cafile=cfg.network.ca_file or None)
    return None
