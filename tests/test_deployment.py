"""Deployment bundles must preserve shared contracts and least-privilege credentials."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from remote_teleop.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_deployment", ROOT / "scripts/prepare_deployment.py")
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


class DeploymentTest(unittest.TestCase):
    def test_scopes_and_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            deployment.prepare(ROOT / "configs/simulation.json", output, hours=1)
            contracts, credentials = [], {}
            for role, scopes in deployment.ROLE_SCOPES.items():
                cfg = AppConfig.model_validate(json.loads((output / f"{role}.json").read_text()))
                contracts.append(cfg.control_contract)
                self.assertTrue(cfg.general.simulation)
                self.assertEqual(cfg.security.credentials_file, f"state/credentials-{role}.json")
                path = output / f"credentials-{role}.json"
                entries = json.loads(path.read_text())["credentials"]
                self.assertEqual({e["role"] for e in entries}, scopes)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                credentials[role] = {e["role"]: e["token"] for e in entries}
            self.assertEqual(contracts[0], contracts[1])
            self.assertEqual(contracts[1], contracts[2])
            self.assertEqual(credentials["cloud"]["client"], credentials["master"]["client"])
            self.assertEqual(credentials["cloud"]["robot"], credentials["slave"]["robot"])
            self.assertFalse((output / "credentials-all.json").exists())
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            before = (output / "credentials-master.json").read_bytes()
            with self.assertRaises(FileExistsError):
                deployment.prepare(ROOT / "configs/simulation.json", output)
            self.assertEqual((output / "credentials-master.json").read_bytes(), before)

    def test_invalid_hours_write_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            with self.assertRaises(ValueError):
                deployment.prepare(ROOT / "configs/simulation.json", output, hours=169)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
