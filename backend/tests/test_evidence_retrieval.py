"""Stage 6: offline retrieval benchmark channels and frozen-corpus contract."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_core import PageTextIndex, _terms  # noqa: E402
from evidence_retrieval import (  # noqa: E402
    apply_scope_filter,
    assemble_context,
    bm25_rank,
    exact_rank,
    rrf_fuse,
    token_stream,
)


class TokenParityTests(unittest.TestCase):
    def test_set_view_matches_current_terms(self):
        samples = [
            "Площадь крепления 526 м2 (площадка КНС)",
            "Грунтовые анкеры 0,44 на 1 м2 закрепляемого геотекстиля",
            "Т-500-К разрывная нагрузка 25/25 кН/м",
            "Зазоры между бетонными блоками засыпаются вручную",
            "г и б к и е б е т о н н ы е",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(set(token_stream(sample)), _terms(sample))

    def test_repetitions_preserved_for_bm25(self):
        self.assertGreater(
            len(token_stream("анкеры анкеры анкеры шт")),
            len(set(token_stream("анкеры анкеры анкеры шт"))))


class ChannelMathTests(unittest.TestCase):
    def test_bm25_constants_and_idf_shape(self):
        import evidence_retrieval as module

        self.assertEqual((module.BM25_K1, module.BM25_B), (1.2, 0.75))
        self.assertEqual(module.RRF_K, 60)
        streams = [token_stream("геотекстиль дорнит 631 м2"),
                   token_stream("анкеры шт 231"),
                   token_stream("бетон м3")]
        self.assertEqual(bm25_rank(streams, "геотекстиль 631 м2"), [0])
        self.assertEqual(bm25_rank(streams, "совершенно другое слово"), [])

    def test_bm25_tie_break_is_page_order(self):
        streams = [token_stream("анкеры шт"), token_stream("анкеры шт")]
        self.assertEqual(bm25_rank(streams, "анкеры шт"), [0, 1])

    def test_exact_needs_pair_or_code(self):
        texts = ["геотекстиль дорнит 631,2 м2 площадка КНС",
                 "анкеры 231 шт площадка КНС",
                 "общий текст без чисел"]
        ranked, details = exact_rank(
            texts, "Геотекстиль Дорнит", "плотностью", "631,2", "м2")
        self.assertEqual(ranked, [0])
        self.assertEqual(details[0][1], 1)
        ranked_none, _ = exact_rank(
            ["просто текст"], "Геотекстиль", "", "631,2", "м2")
        self.assertEqual(ranked_none, [])

    def test_rrf_equal_weights_stable_order(self):
        self.assertEqual(rrf_fuse([[2, 0], [0, 1], [1, 0]]), [0, 1, 2])
        self.assertEqual(rrf_fuse([[], []]), [])


class ScopeFilterTests(unittest.TestCase):
    def test_drops_rows_not_dual_pages(self):
        fragment = ("Площадь крепления - 526м2 (площадка КНС) "
                    "и 1879м2 (площадка ЛОС).\n"
                    "Ведомость площадки ЛОС 2254 м2\n"
                    "Укладка геотекстиля")
        kept, dropped, ambiguous = apply_scope_filter(fragment, "КНС", ["ЛОС"])
        self.assertIn("526м2", kept)
        self.assertNotIn("Ведомость площадки ЛОС", kept)
        self.assertEqual(dropped, 1)
        self.assertFalse(ambiguous)

    def test_unknown_scope_flags_ambiguous_without_filtering(self):
        fragment = "Площадь КНС 526 м2\nПлощадь ЛОС 1879 м2"
        kept, dropped, ambiguous = apply_scope_filter(
            fragment, "", ["КНС", "ЛОС"])
        self.assertEqual(kept, fragment)
        self.assertEqual(dropped, 0)
        self.assertTrue(ambiguous)


class FrozenCorpusTests(unittest.TestCase):
    def test_snapshot_round_trip_and_hash_rejection(self):
        index = PageTextIndex(("стр одна текст", "стр два текст"),
                              {1: "Сбой чтения"}, (1,),
                              ((), ("ячейка",)), None,
                              quality={1: {"ocr": "failed"}}, metadata={"a": 1})
        snapshot = index.to_snapshot()
        self.assertIn("corpus_id", snapshot)
        restored = PageTextIndex.from_snapshot(snapshot)
        self.assertEqual(restored.to_snapshot(), snapshot)
        tampered = dict(snapshot)
        tampered["texts"] = ["подмена"]
        with self.assertRaises(ValueError):
            PageTextIndex.from_snapshot(tampered)

    def test_assemble_context_records_ranking_and_budgets(self):
        from evidence_retrieval import MAX_CONTEXT_CHARS, MAX_PAGES

        texts = tuple("позиция %d геотекстиль 100 м2 площадка КНС" % i
                      for i in range(6))
        index = PageTextIndex(texts)
        assembly = assemble_context(
            index, [5, 4, 3, 2, 1, 0], [0], "геотекстиль", "", "",
            "100", "", "КНС", ["ЛОС"])
        self.assertEqual(len(assembly["ordered_1based"]), MAX_PAGES)
        self.assertLessEqual(assembly["context_chars"], MAX_CONTEXT_CHARS)
        self.assertEqual(len(assembly["ranking_1based"]), 6)
        self.assertNotEqual(assembly["ranking_1based"],
                            assembly["ordered_1based"])


class AdversarialMutationTests(unittest.TestCase):
    def test_wrong_group_quantity_does_not_confirm(self):
        from evidence_retrieval import evaluate_chain

        texts = ("Площадь крепления - 526м2 (площадка КНС) "
                 "и 1879м2 (площадка ЛОС).",)
        index = PageTextIndex(texts)
        assembly = assemble_context(
            index, [0], [], "Укладка экрана", "", "", "310,035", "",
            "ЛОС", ["КНС"])
        evidence = {"id": "PLAN-AREAS", "page": 1, "type": "native_quote",
                    "quote": ("Площадь крепления - 526м2 (площадка КНС) "
                              "и 1879м2 (площадка ЛОС).")}
        chain = evaluate_chain(
            "PLAN-AREAS", evidence, index, [0], assembly, "310,035", "м3")
        self.assertFalse(chain.get("sufficient"))
        self.assertIn(chain.get("loss"), {
            "validation_rejected", "measurement_basis_unresolved",
            "binding_unresolved", "context_dropped"})


if __name__ == "__main__":
    unittest.main()
