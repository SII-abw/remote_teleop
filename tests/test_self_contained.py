"""Keep source independence and old configuration compatibility under regression."""

import ast
from pathlib import Path
import unittest

from remote_teleop.config import AppConfig


class SelfContainedTests(unittest.TestCase):
    def test_no_external_source_imports(self):
        package = Path(__file__).resolve().parents[1] / "src/remote_teleop"
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    modules = [node.module or ""]
                for module in modules:
                    self.assertNotEqual(module.split(".")[0], "lerobot", str(path))

    def test_existing_source_path_is_ignored_and_not_persisted(self):
        original = AppConfig()
        values = original.model_dump()
        values["policy"]["lerobot_source"] = "/nonexistent/external/checkout"
        parsed = AppConfig.model_validate(values)
        self.assertEqual(original.control_contract, parsed.control_contract)
        self.assertNotIn("lerobot_source", parsed.policy.model_dump())
        self.assertNotIn("lerobot_source", parsed.policy.model_json_schema()["properties"])


if __name__ == "__main__":
    unittest.main()
