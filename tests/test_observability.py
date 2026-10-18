"""Issue 21: trace correlation, structured logs, redaction, upload limits, metrics."""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_models import AuditSettings
from audit_observability import (
    RunMetrics,
    bind,
    current,
    estimate_cost,
    get_logger,
    reset,
    restore,
    scrub,
    snapshot,
)
from audit_tracing import Observation, TracingRun, span


class CorrelationTests(unittest.TestCase):
    def test_position_trace_carries_run_and_request_ids(self):
        seen = {}

        class FakeRoot:
            trace_id = "trace-1"
            id = "span-1"

            def update(self, **kwargs):
                seen["update"] = kwargs

            def end(self):
                seen["ended"] = True

        class FakeClient:
            def start_observation(self, **kwargs):
                seen["start"] = kwargs
                return FakeRoot()

            def flush(self):
                seen["flushed"] = True

            def get_trace_url(self, trace_id):
                return f"https://langfuse/{trace_id}"

        with patch("audit_tracing._configured_client", return_value=FakeClient()):
            run = TracingRun("pk", "sk", audit_run_id="run-123", request_id="req-456")
            trace = run.position({"work": "Кабель"}, {"position_index": 0})
            trace.finish({"status": "ОК"})
            run.close()
        metadata = seen["start"]["metadata"]
        self.assertEqual(metadata["audit_run_id"], "run-123")
        self.assertEqual(metadata["request_id"], "req-456")
        self.assertEqual(metadata["position_index"], 0)
        self.assertTrue(seen["ended"])
        self.assertTrue(seen["flushed"])
        self.assertEqual(trace.trace_id, "trace-1")

    def test_explicit_metadata_wins_over_run_defaults(self):
        seen = {}

        class FakeRoot:
            trace_id = "t"
            id = "s"

            def end(self):
                pass

        class FakeClient:
            def start_observation(self, **kwargs):
                seen["start"] = kwargs
                return FakeRoot()

            def flush(self):
                pass

        with patch("audit_tracing._configured_client", return_value=FakeClient()):
            run = TracingRun("pk", "sk", audit_run_id="run-123")
            run.position({}, {"audit_run_id": "explicit"})
            run.close()
        self.assertEqual(seen["start"]["metadata"]["audit_run_id"], "explicit")


class StructuredLogTests(unittest.TestCase):
    def test_json_record_carries_ids_and_scrubs_secrets(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.root
        old_handlers = list(root.handlers)
        old_level = root.level
        try:
            root.handlers = [handler]
            root.setLevel(logging.INFO)
            logger = get_logger("test-issue-21-logs")
            token = bind(audit_run_id="run-1", position_index=3, stage="audit")
            try:
                logger.info("done", extra={"api_key": "SECRET-VALUE", "status": "ОК"})
            finally:
                reset(token)
        finally:
            root.handlers = old_handlers
            root.setLevel(old_level)
        lines = stream.getvalue().strip().splitlines()
        self.assertTrue(lines, "expected a JSON log line")
        payload = json.loads(lines[-1])
        self.assertEqual(payload["audit_run_id"], "run-1")
        self.assertEqual(payload["position_index"], 3)
        self.assertEqual(payload["stage"], "audit")
        self.assertNotIn("SECRET-VALUE", stream.getvalue())

    def test_context_snapshot_restores_in_another_thread(self):
        token = bind(audit_run_id="run-9", request_id="req-9")
        try:
            fields = snapshot()
        finally:
            reset(token)
        self.assertEqual(current(), {})
        token2 = restore(fields)
        try:
            self.assertEqual(current()["audit_run_id"], "run-9")
        finally:
            reset(token2)

    def test_scrub_replaces_nested_secrets(self):
        cleaned = scrub(
            {"api_key": "x", "nested": [{"token": "y", "ok": 1}], "plain": "z"}
        )
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertEqual(cleaned["nested"][0]["token"], "[redacted]")
        self.assertEqual(cleaned["nested"][0]["ok"], 1)
        self.assertEqual(cleaned["plain"], "z")


class RedactionTests(unittest.TestCase):
    def _capture(self, **flags):
        seen = {}

        class FakeObs:
            def update(self, **kwargs):
                seen["update"] = kwargs

        class FakeClient:
            @contextmanager
            def start_as_current_observation(self, **kwargs):
                seen["start"] = kwargs
                yield FakeObs()

        active = ContextVar("test_redact_client", default=FakeClient())
        redact = ContextVar("test_redact_flag", default=flags.get("redact", False))
        mask = ContextVar("test_mask_flag", default=flags.get("mask", False))
        return seen, active, redact, mask

    def test_redact_pdf_replaces_fragments_keeps_routing(self):
        seen, active, redact, mask = self._capture(redact=True)
        with (
            patch("audit_tracing._client", active),
            patch("audit_tracing._redact", redact),
            patch("audit_tracing._mask", mask),
        ):
            with span("select-context", input={1: "секретный текст ПД"}) as obs:
                obs.update(output={2: "другой секрет"})
        self.assertEqual(seen["start"]["input"], {1: "[redacted 18 chars]"})
        self.assertEqual(seen["update"]["output"], {2: "[redacted 13 chars]"})

    def test_mask_prompts_replaces_only_message_content(self):
        seen, active, redact, mask = self._capture(mask=True)
        messages = [
            {"role": "system", "content": "инструкция"},
            {"role": "user", "content": "промпт с ПД"},
        ]
        with (
            patch("audit_tracing._client", active),
            patch("audit_tracing._redact", redact),
            patch("audit_tracing._mask", mask),
        ):
            with span("generate-audit", input=messages):
                pass
        self.assertEqual(
            seen["start"]["input"],
            [
                {"role": "system", "content": "[redacted prompt]"},
                {"role": "user", "content": "[redacted prompt]"},
            ],
        )

    def test_flags_off_leaves_payloads_untouched(self):
        seen, active, redact, mask = self._capture()
        with (
            patch("audit_tracing._client", active),
            patch("audit_tracing._redact", redact),
            patch("audit_tracing._mask", mask),
        ):
            with span("generate-audit", input=[{"role": "user", "content": "x"}]):
                pass
        self.assertEqual(seen["start"]["input"], [{"role": "user", "content": "x"}])

    def test_observation_update_is_redacted_by_stage_name(self):
        seen = {}

        class FakeObs:
            def update(self, **kwargs):
                seen.update(kwargs)

        redact = ContextVar("test_obs_redact", default=True)
        with patch("audit_tracing._redact", redact):
            Observation(FakeObs(), name="read-ocr-page").update(
                output={"text": "секрет", "cache_hit": True}
            )
        self.assertEqual(seen["output"]["text"], "[redacted 6 chars]")
        self.assertTrue(seen["output"]["cache_hit"])

    def test_redact_keeps_result_flags_redacts_quotes(self):
        seen, active, redact, mask = self._capture(redact=True)
        result = {
            "status": "Ошибка",
            "work_found": True,
            "requests": 2,
            "discrepancy_details": "секретные детали",
            "evidence": [{"page": 1, "quote": "секретная цитата"}],
        }
        with (
            patch("audit_tracing._client", active),
            patch("audit_tracing._redact", redact),
            patch("audit_tracing._mask", mask),
        ):
            with span("confirm-quantities", input=result):
                pass
        redacted = seen["start"]["input"]
        self.assertEqual(redacted["status"], "Ошибка")
        self.assertTrue(redacted["work_found"])
        self.assertEqual(redacted["requests"], 2)
        self.assertEqual(redacted["discrepancy_details"], "[redacted 16 chars]")
        self.assertEqual(
            redacted["evidence"], [{"page": 1, "quote": "[redacted 16 chars]"}]
        )


class UploadSecurityTests(unittest.TestCase):
    def _context(self, **overrides):
        from api import ApiContext
        from storage_blobs import FileBlobStore

        tmp = tempfile.mkdtemp(prefix="smeta-21-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        settings = AuditSettings(database_url="x", blob_store_url=f"file://{tmp}")
        for key, value in overrides.items():
            object.__setattr__(settings, key, value)
        return ApiContext(
            settings=settings,
            repository=SimpleNamespace(),  # type: ignore[arg-type]
            blob_store=FileBlobStore(tmp),
        )

    def test_path_traversal_filename_is_sanitized(self):
        from api import _safe_filename

        self.assertEqual(_safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(_safe_filename("C:\\docs\\a.pdf"), "a.pdf")

    def test_blank_filename_rejected(self):
        from api import ApiError, _safe_filename

        for bad in ("", "   ", "..", "."):
            with self.assertRaises(ApiError):
                _safe_filename(bad)

    def test_oversize_upload_rejected(self):
        from api import ApiError, _check_upload_limits

        context = self._context(max_upload_bytes=10)
        with self.assertRaises(ApiError) as raised:
            _check_upload_limits(context, b"x" * 11)
        self.assertEqual(raised.exception.code, "upload_too_large")

    def test_wrong_mime_rejected_octet_stream_allowed(self):
        from api import ApiError, _check_upload_mime

        context = self._context()
        _ = context
        bad = SimpleNamespace(filename="a.pdf", content_type="image/png")
        with self.assertRaises(ApiError) as raised:
            _check_upload_mime(bad, "pdf")
        self.assertEqual(raised.exception.code, "unsupported_media_type")
        generic = SimpleNamespace(
            filename="a.pdf", content_type="application/octet-stream"
        )
        _check_upload_mime(generic, "pdf")

    def test_magic_bytes_verified(self):
        from api import ApiError, _check_pdf_magic, _check_vor_magic

        _check_pdf_magic(b"%PDF-1.7 rest")
        with self.assertRaises(ApiError):
            _check_pdf_magic(b"not a pdf")
        _check_vor_magic(b"PK\x03\x04", "a.xlsx")
        with self.assertRaises(ApiError):
            _check_vor_magic(b"plain", "a.xlsx")
        with self.assertRaises(ApiError):
            _check_vor_magic(b"a\x00b", "a.csv")

    def test_blob_get_rejects_escape(self):
        from storage_blobs import FileBlobStore

        with tempfile.TemporaryDirectory(prefix="smeta-21-blobs-") as tmp:
            store = FileBlobStore(tmp)
            ref = store.put(b"data")
            self.assertEqual(store.get(ref.key), b"data")
            for bad in ("../../etc/passwd", "..\\..\\x", "", "a/b/c/d/e"):
                with self.assertRaises(FileNotFoundError):
                    store.get(bad)


class LlmPolicyTests(unittest.TestCase):
    def test_disallowed_host_fails_closed(self):
        from audit_llm import _check_llm_host

        _check_llm_host(
            "https://ai.api.cloud.yandex.net/v1", ("ai.api.cloud.yandex.net",)
        )
        with self.assertRaises(ValueError):
            _check_llm_host("https://evil.example/v1", ("ai.api.cloud.yandex.net",))
        with self.assertRaises(ValueError):
            _check_llm_host(
                "https://ai.api.cloud.yandex.net.evil.example/v1",
                ("ai.api.cloud.yandex.net",),
            )
        with self.assertRaises(ValueError):
            _check_llm_host("not a url", ("ai.api.cloud.yandex.net",))

    def test_settings_allowlist_defaults_to_yandex(self):
        settings = AuditSettings()
        self.assertEqual(settings.allowed_llm_hosts, ("ai.api.cloud.yandex.net",))

    def test_settings_loader_reads_issue_21_keys(self):
        from unittest.mock import patch as mock_patch

        with mock_patch.dict(os.environ, {}, clear=True):
            settings = AuditSettings.load(
                file_values={
                    "AUDIT_TELEMETRY_REDACT_PDF": "1",
                    "AUDIT_TELEMETRY_MASK_PROMPTS": "true",
                    "AUDIT_MAX_UPLOAD_BYTES": "1024",
                    "AUDIT_ALLOWED_LLM_HOSTS": "a.example, b.example",
                    "AUDIT_LLM_INPUT_COST_PER_1K": "0.5",
                    "AUDIT_LLM_OUTPUT_COST_PER_1K": "1.5",
                },
                secrets=None,
            )
        self.assertTrue(settings.telemetry_redact_pdf)
        self.assertTrue(settings.telemetry_mask_prompts)
        self.assertEqual(settings.max_upload_bytes, 1024)
        self.assertEqual(settings.allowed_llm_hosts, ("a.example", "b.example"))
        self.assertEqual(settings.llm_input_cost_per_1k, 0.5)
        self.assertEqual(settings.llm_output_cost_per_1k, 1.5)


class MetricsTests(unittest.TestCase):
    def test_estimate_cost_none_without_split_or_rates(self):
        self.assertIsNone(estimate_cost({"total": 100}, 1.0, 2.0))
        self.assertIsNone(estimate_cost({"input": 1, "output": 1}, 0.0, 2.0))
        self.assertIsNone(estimate_cost({"input": 1, "output": 1}, 1.0, 0.0))
        self.assertEqual(estimate_cost({"input": 1000, "output": 500}, 2.0, 4.0), 4.0)

    def test_run_metrics_summary(self):
        metrics = RunMetrics(run_id="run-1")
        metrics.record_stage("prepare", 1.5)
        metrics.record_stage("prepare", 0.5)
        metrics.record_position(
            {"status": "ОК", "requests": 2, "contexts": 1},
            usage={"input": 100, "output": 50},
            cost=0.5,
        )
        metrics.record_position({"status": "Ошибка", "requests": 1, "contexts": 1})
        metrics.record_error("timeout")
        summary = metrics.finish()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["contexts"], 2)
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 50)
        self.assertEqual(summary["estimated_cost"], 0.5)
        self.assertEqual(summary["statuses"], {"ОК": 1, "Ошибка": 1})
        self.assertEqual(summary["errors"], {"timeout": 1})
        self.assertEqual(summary["stage_seconds"], {"prepare": 2.0})
        self.assertGreaterEqual(summary["elapsed_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
