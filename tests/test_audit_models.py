"""Typed domain models: VOR/LLM/audit validation, serialization, legacy compat."""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from audit_models import (
    AuditResult,
    AuditSettings,
    AuditStatus,
    ErrorKind,
    Evidence,
    EvidenceKind,
    VorMeta,
    VorPosition,
)
from audit_reports import build_user_report
from vor_parse import load_vor_positions


class VorModelTests(unittest.TestCase):
    def test_positions_validate_and_expose_binding_refs(self):
        csv = (
            "№ п.п.;Наименование работ;Объём работ / Количество;Ед. изм.;"
            "Ссылка на ПД;Наименование файла;Номер страниц (через пробел)\n"
            "1;Copper cable;45;m;;project.pdf;1\n"
        )

        class FakeFile(io.BytesIO):
            def getvalue(self):
                return super().getvalue()

        positions, meta = load_vor_positions(
            FakeFile(csv.encode("utf-8-sig")), "vor.csv"
        )
        self.assertIsInstance(meta, VorMeta)
        self.assertEqual(meta.doc_number, "vor.csv")
        self.assertEqual(meta.get("basis_code"), "Проектная документация")
        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertIsInstance(position, VorPosition)
        self.assertEqual(position.work_name, "Copper cable")
        self.assertEqual(position.explicit_refs, ["project.pdf"])
        # Legacy task readers keep working through the validated view.
        task = position.legacy_task()
        self.assertEqual(task["w_name"], "Copper cable")
        self.assertEqual(
            VorPosition.from_legacy_task(task).model_dump(), position.model_dump()
        )

    def test_missing_required_columns_are_explicit(self):
        csv = "№ п.п.;Наименование работ\n1;Copper cable\n"

        class FakeFile(io.BytesIO):
            def getvalue(self):
                return super().getvalue()

        with self.assertRaises(ValueError) as raised:
            load_vor_positions(FakeFile(csv.encode("utf-8-sig")), "vor.csv")
        self.assertIn("обязательные колонки", str(raised.exception))

    def test_invalid_position_pages_rejected(self):
        with self.assertRaises(ValueError):
            VorPosition(
                index=0,
                work_name="Cable",
                volume="45",
                unit="m",
                target_indices=[-1],
            )
        with self.assertRaises(ValueError):
            VorPosition(
                index=0,
                work_name="Cable",
                volume="45",
                unit="m",
                primary_pages=[0],
            )


class EvidenceModelTests(unittest.TestCase):
    def test_document_page_and_fragment_required(self):
        evidence = Evidence(
            document_id="project.pdf", page=2, quote="Cable", kind="work"
        )
        self.assertEqual(evidence.kind, EvidenceKind.WORK)
        dumped = evidence.model_dump()
        self.assertEqual(
            dumped,
            {"document_id": "project.pdf", "page": 2, "quote": "Cable", "kind": "work"},
        )
        self.assertEqual(Evidence(**json.loads(evidence.model_dump_json())), evidence)

    def test_legacy_quote_without_document_still_validates(self):
        evidence = Evidence.from_legacy({"page": "3", "quote": "Cable", "kind": "work"})
        self.assertEqual(evidence.page, 3)
        self.assertEqual(evidence.document_id, "")
        for bad in (
            {"page": 0, "quote": "Cable", "kind": "work"},
            {"page": 1, "quote": "  ", "kind": "work"},
            {"page": 1, "quote": "Cable", "kind": "guess"},
            {"page": 1, "quote": "x" * 1201, "kind": "work"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Evidence.from_legacy(bad)


class AuditResultModelTests(unittest.TestCase):
    def test_ok_requires_all_confirmations(self):
        with self.assertRaises(ValueError):
            AuditResult(
                status="ОК",
                work_found=True,
                volume_confirmed=False,
                unit_confirmed=True,
                comment_confirmed=True,
            )

    def test_missing_work_keeps_explicit_unknowns(self):
        result = AuditResult(
            status="Ошибка",
            work_found=False,
            volume_confirmed=False,
            unit_confirmed=False,
            comment_confirmed=False,
            found_volume="45",
            found_unit="m",
            found_page="2",
        )
        self.assertEqual(result.found_page, "-")
        self.assertEqual(result.found_volume, "-")
        self.assertEqual(result.found_unit, "-")

    def test_legacy_roundtrip_preserves_shape(self):
        legacy = {
            "status": "Ошибка",
            "work_found": True,
            "volume_confirmed": False,
            "unit_confirmed": True,
            "comment_confirmed": True,
            "found_volume": "40",
            "found_unit": "m",
            "found_page": "2",
            "found_pages": [2],
            "evidence": [{"page": 2, "quote": "Cable 40 m", "kind": "work"}],
            "discrepancy_details": "mismatch",
            "search_complete": True,
            "requests": 1,
            "contexts": 1,
        }
        result = AuditResult.from_legacy(legacy)
        self.assertEqual(result.status, AuditStatus.ERROR)
        back = result.to_legacy()
        for key, value in legacy.items():
            self.assertEqual(back[key], value)
        self.assertEqual(AuditResult.from_legacy(back).to_legacy(), back)
        # JSON serialization never leaks model internals.
        self.assertEqual(
            json.loads(result.model_dump_json())["evidence"][0]["kind"], "work"
        )

    def test_report_accepts_models_and_dicts_identically(self):
        frame = pd.DataFrame([{"№": 1, "Статус аудита": "ОК"}])
        source = {
            "pdf_name": "кр.pdf",
            "found_page": 18,
            "found_pages": [18],
            "vor_pages": [18],
            "work_found": True,
            "volume_confirmed": True,
            "unit_confirmed": True,
            "comment_confirmed": True,
            "found_volume": "120",
            "found_unit": "м³",
        }
        task = VorPosition(
            index=0,
            work_name="Бетонные работы",
            volume="120",
            unit="м³",
            page_ref="18",
            file_ref="КР.pdf",
            target_indices=[17],
            location_description="Стр. 18",
            primary_pages=[18],
        )
        expected = build_user_report(frame, [source], [task.legacy_task()])
        result = AuditResult.from_legacy(
            {
                **source,
                "status": "ОК",
                "found_page": "18",
                "evidence": [],
                "discrepancy_details": "",
                "search_complete": True,
            }
        )
        actual = build_user_report(frame, [result], [task])
        pd.testing.assert_frame_equal(actual, expected)


class AuditSettingsModelTests(unittest.TestCase):
    def test_env_resolution_and_defaults(self):
        with patch.dict(
            os.environ,
            {"YANDEX_CLOUD_FOLDER": "folder", "YANDEX_CLOUD_API_KEY": "key"},
            clear=True,
        ):
            settings = AuditSettings.load(file_values={}, secrets=None)
        self.assertEqual(settings.yandex_cloud_folder, "folder")
        self.assertEqual(settings.yandex_cloud_api_key, "key")
        self.assertEqual(settings.yandex_cloud_model, "qwen3.6-35b-a3b/latest")
        self.assertEqual(settings.langfuse_base_url, "https://cloud.langfuse.com")

    def test_error_kinds_are_explicit(self):
        self.assertEqual(
            {kind.value for kind in ErrorKind},
            {"bind", "prep", "validation", "provider", "budget", "internal"},
        )


if __name__ == "__main__":
    unittest.main()
