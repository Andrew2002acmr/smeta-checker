"""Offline baseline: fixed reference audit without LLM API or secrets."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from baseline_audit import (build_inputs, compare_summaries, main as baseline_main,
                            logical_input_hash, run_baseline)
from audit_reports import DISCREPANCY, MANUAL, NOT_CHECKED


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ):
            for key in ("YANDEX_CLOUD_API_KEY", "YANDEX_CLOUD_FOLDER",
                        "YANDEX_CLOUD_MODEL", "LANGFUSE_PUBLIC_KEY",
                        "LANGFUSE_SECRET_KEY"):
                os.environ.pop(key, None)
            cls.summary = run_baseline()

    def test_statuses_pages_volumes_units(self):
        expected = [
            ("ОК", "Стр. 2", "45", "m", [2]),
            ("ОК", "Стр. 2", "45", "m", [2]),
            ("Ошибка", "Стр. 3", "2", "pcs", [3]),
            ("Неполная проверка", "-", "-", "-", []),
            ("Технический сбой", "-", "-", "-", []),
        ]
        self.assertEqual(len(self.summary["cases"]), 5)
        for case, (status, page, volume, unit, pages) in zip(self.summary["cases"], expected):
            with self.subTest(position=case["position"]):
                self.assertEqual(case["status"], status)
                self.assertEqual(case["page"], page)
                self.assertEqual(case["found_volume"], volume)
                self.assertEqual(case["found_unit"], unit)
                self.assertEqual(case["found_pages"], pages)

    def test_confirmations_and_completion(self):
        expected = [
            (True, True, True, True, True),
            (True, True, True, True, True),
            (True, False, True, True, True),
            (False, False, False, False, False),
            (False, False, False, False, False),
        ]
        for case, flags in zip(self.summary["cases"], expected):
            with self.subTest(position=case["position"]):
                self.assertEqual(
                    (case["work_found"], case["volume_confirmed"],
                     case["unit_confirmed"], case["comment_confirmed"],
                     case["search_complete"]), flags)
                self.assertGreaterEqual(case["requests"], 1)
                self.assertLessEqual(case["requests"], 6)
                self.assertGreaterEqual(case["contexts"], 1)

    def test_proofs_quote_supplied_fragments(self):
        by_pos = {c["position"]: c for c in self.summary["cases"]}
        for position in ("1", "2"):
            evidence = by_pos[position]["evidence"]
            self.assertEqual([(e["page"], e["kind"]) for e in evidence],
                             [(2, "work"), (2, "volume"), (2, "unit")])
            quotes = " ".join(e["quote"] for e in evidence)
            self.assertIn("Copper cable", quotes)
            self.assertIn("45", quotes)
        evidence = by_pos["3"]["evidence"]
        self.assertTrue(all(e["page"] == 3 for e in evidence))
        quotes = " ".join(e["quote"] for e in evidence)
        self.assertIn("Circulating pump", quotes)
        self.assertIn("2", quotes)
        self.assertIn("Volume differs", by_pos["3"]["details"])
        self.assertEqual(by_pos["4"]["evidence"], [])
        self.assertIn("не доказывает", by_pos["4"]["details"])
        self.assertEqual(by_pos["5"]["evidence"], [])
        self.assertIn("Simulated provider failure", by_pos["5"]["details"])

    def test_user_exports_match_and_prioritize(self):
        expected = {"1": "ОК", "2": "ОК", "3": DISCREPANCY,
                    "4": MANUAL, "5": NOT_CHECKED}
        for case in self.summary["cases"]:
            with self.subTest(position=case["position"]):
                self.assertTrue(case["exports_match"])
                self.assertEqual(case["excel_status"], expected[case["position"]])

    def test_offline_no_real_api_or_secrets(self):
        self.assertTrue(self.summary["offline"])
        self.assertEqual(self.summary["api_calls"], 5)
        self.assertEqual(self.summary["fake_calls"], 5)
        for key in ("python", "streamlit", "openai", "pymupdf", "pandas"):
            self.assertNotEqual(self.summary["versions"].get(key), "?")
        self.assertEqual(len(self.summary["logical_hash"]), 64)

    def test_inputs_regenerate_identically(self):
        with tempfile.TemporaryDirectory(prefix="smeta-baseline-a-") as first, \
                tempfile.TemporaryDirectory(prefix="smeta-baseline-b-") as second:
            build_inputs(Path(first))
            build_inputs(Path(second))
            self.assertEqual(logical_input_hash(Path(first)),
                             logical_input_hash(Path(second)))

    def test_compare_identical_is_clean(self):
        diff = compare_summaries(self.summary, copy.deepcopy(self.summary))
        self.assertTrue(diff["identical_inputs"])
        self.assertFalse(diff["changed"])
        self.assertEqual(diff["changes"], [])

    def test_compare_shows_status_evidence_and_budget_changes(self):
        new = copy.deepcopy(self.summary)
        new["cases"][0]["status"] = "Ошибка"
        new["cases"][2]["evidence"] = []
        new["cases"][3]["status"] = "Ошибка"
        new["cases"][4]["status"] = "Неполная проверка"
        new["cases"][4]["requests"] = 6
        diff = compare_summaries(self.summary, new)
        self.assertTrue(diff["identical_inputs"])
        self.assertTrue(diff["changed"])
        changed = {(c["position"], c["field"]) for c in diff["changes"]}
        for expected in (("1", "status"), ("3", "evidence"), ("4", "status"),
                         ("5", "status"), ("5", "requests")):
            self.assertIn(expected, changed)
        self.assertEqual(diff["totals"]["old_incomplete"], 1)
        self.assertEqual(diff["totals"]["new_incomplete"], 1)
        self.assertEqual(diff["totals"]["old_technical"], 1)
        self.assertEqual(diff["totals"]["new_technical"], 0)
        rows = {row["position"]: row for row in diff["rows"]}
        self.assertTrue(rows["1"]["changed"])
        self.assertFalse(rows["2"]["changed"])


    def test_compare_cli_writes_report_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="smeta-baseline-diff-") as folder:
            folder = Path(folder)
            old = folder / "old.json"
            new = folder / "new.json"
            old.write_text(json.dumps(self.summary, ensure_ascii=False), encoding="utf-8")
            changed = copy.deepcopy(self.summary)
            changed["cases"][3]["status"] = "Ошибка"
            new.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            out = folder / "out"
            self.assertEqual(baseline_main(["--output", str(out), "--compare",
                                            str(old), str(new)]), 1)
            report = (out / "comparison.md").read_text(encoding="utf-8")
            self.assertIn("Неполные проверки: 1 → 0", report)
            self.assertIn("Технические сбои: 1 → 1", report)
            self.assertEqual(baseline_main(["--output", str(out), "--compare",
                                            str(old), str(old)]), 0)

if __name__ == "__main__":
    unittest.main()
