"""Settings must use the current project .env rather than a stale process value."""

import ast
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values


def read_setting(values, secrets=None):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_setting")
    namespace = {"os": os, "project_env": values, "st": SimpleNamespace(secrets=secrets or {})}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), namespace)
    return namespace["get_setting"]


class SettingsTests(unittest.TestCase):
    def test_updated_dotenv_overrides_old_process_key(self):
        with tempfile.TemporaryDirectory(prefix="smeta-settings-") as folder, patch.dict(os.environ, {"AGNES_API_KEY": "old-process-test-key"}):
            path = Path(folder) / ".env"
            path.write_text("AGNES_API_KEY=first-test-key\n", encoding="utf-8-sig")
            self.assertEqual(read_setting(dotenv_values(path, encoding="utf-8-sig"))("AGNES_API_KEY"), "first-test-key")
            path.write_text("AGNES_API_KEY=updated-test-key\n", encoding="utf-8-sig")
            self.assertEqual(read_setting(dotenv_values(path, encoding="utf-8-sig"))("AGNES_API_KEY"), "updated-test-key")

    def test_no_local_value_uses_environment(self):
        with patch.dict(os.environ, {"AGNES_API_KEY": "environment-test-key"}):
            self.assertEqual(read_setting({}, {"AGNES_API_KEY": "secret-test-key"})("AGNES_API_KEY"), "environment-test-key")

    def test_secrets_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(read_setting({}, {"AGNES_API_KEY": "secret-test-key"})("AGNES_API_KEY"), "secret-test-key")

    def test_explicit_empty_file_value_clears_old_key(self):
        with patch.dict(os.environ, {"AGNES_API_KEY": "old-test-key"}):
            self.assertEqual(read_setting({"AGNES_API_KEY": ""})("AGNES_API_KEY"), "")


if __name__ == "__main__":
    unittest.main()
