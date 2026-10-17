"""Settings must use the current project .env rather than a stale process value."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from audit_settings import get_setting as _resolve_setting


def read_setting(values, secrets=None):
    return partial(_resolve_setting, file_values=values, secrets=secrets or {})


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
