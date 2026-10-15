"""Observability must not change audit results or hide original failures."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_tracing import span
import test_audit


class TracingIsolationTests(unittest.TestCase):
    def test_broken_exporter_does_not_turn_verified_work_into_failure(self):
        class BrokenClient:
            def start_as_current_observation(self, **kwargs):
                raise ConnectionError("Tracing unavailable")

        active = ContextVar("test_tracing_client", default=BrokenClient())
        with patch("audit_tracing._client", active), self.assertLogs("audit_tracing", level="WARNING"):
            result, _ = test_audit.AdapterTests().run_adapter([test_audit.AdapterTests.valid()])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_pages"], [1])
        self.assertEqual(result["found_volume"], "45")
        self.assertEqual(result["requests"], 1)

    def test_exporter_close_failure_does_not_replace_validation_failure(self):
        class BrokenClient:
            @contextmanager
            def start_as_current_observation(self, **kwargs):
                try:
                    yield SimpleNamespace(update=lambda **kw: None)
                finally:
                    raise ConnectionError("Exporter close failed")

        original = ValueError("Quote not present on the supplied page")
        active = ContextVar("test_tracing_client", default=BrokenClient())
        with patch("audit_tracing._client", active), self.assertLogs("audit_tracing", level="WARNING"):
            with self.assertRaises(ValueError) as raised:
                with span("validate-audit-response"):
                    raise original
        self.assertIs(raised.exception, original)


if __name__ == "__main__":
    unittest.main()
