"""Observability must not change audit results or hide original failures."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_tracing import STAGE_DESCRIPTIONS, span
from audit_core import INCOMPLETE, TECHNICAL_ERROR, result_level
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


class CaptureClient:
    def __init__(self):
        self.kwargs = None

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.kwargs = kwargs
        yield SimpleNamespace(update=lambda **kw: None)


class StageDescriptionTests(unittest.TestCase):
    def test_description_injected_and_explicit_metadata_preserved(self):
        client = CaptureClient()
        active = ContextVar("test_tracing_descriptions", default=client)
        with patch("audit_tracing._client", active):
            with span("locate-work", metadata={"stage": "search"}):
                pass
        self.assertEqual(client.kwargs["metadata"],
                         {"description_ru": STAGE_DESCRIPTIONS["locate-work"], "stage": "search"})

    def test_unknown_stage_leaves_metadata_untouched(self):
        client = CaptureClient()
        active = ContextVar("test_tracing_unknown", default=client)
        with patch("audit_tracing._client", active):
            with span("mystery-stage"):
                pass
        self.assertNotIn("metadata", client.kwargs)

    def test_result_level_maps_audit_outcomes(self):
        self.assertEqual(result_level(TECHNICAL_ERROR), "ERROR")
        self.assertEqual(result_level(INCOMPLETE), "WARNING")
        self.assertEqual(result_level("ОК"), "DEFAULT")
        self.assertEqual(result_level("Ошибка"), "DEFAULT")


class TokenUsageTests(unittest.TestCase):
    def test_reasoning_is_a_subset_not_added_to_provider_total(self):
        from audit_tracing import token_usage
        for usage in (
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
             "completion_tokens_details": {"reasoning_tokens": 17}},
            SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=17)),
        ):
            self.assertEqual(token_usage(usage),
                             {"input": 10, "output": 20, "total": 30, "reasoning": 17})

    def test_unknown_reasoning_is_not_reported_as_zero(self):
        from audit_tracing import token_usage
        self.assertNotIn("reasoning", token_usage(SimpleNamespace(total_tokens=30)))
        self.assertEqual(token_usage({"completion_tokens_details": {"reasoning_tokens": 0}}),
                         {"reasoning": 0})

if __name__ == "__main__":
    unittest.main()
