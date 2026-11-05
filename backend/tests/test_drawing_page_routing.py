from types import SimpleNamespace

import pytest

from pdf_page_routing import deferred_drawing_errors, coverage_for_pages


def page(text, error="scan_table_failed:TimeoutExpired: 90 seconds"):
    return SimpleNamespace(texts=[text], errors={0: error})


def test_titled_profile_timeout_is_deferred_without_erasing_the_error():
    index = page("УСЛОВНЫЕ ОБОЗНАЧЕНИЯ\nПродольный профиль\nПК 5+00, отметка 120.53")
    routed = deferred_drawing_errors(index)
    assert routed[0]["kind"] == "drawing_analysis_pending"
    assert routed[0]["absence_of_project_fact_not_proven"]
    assert index.errors == {0: "scan_table_failed:TimeoutExpired: 90 seconds"}


@pytest.mark.parametrize("text", [
    "Продольный профиль\nВедомость объемов работ\nНаименование Количество",
    "Продольный профиль\nСпецификация материалов",
    "Продольный профиль\nНаименование\nКоличество",
    "Ведомость работ\nНаименование Количество 114 шт.",
    "Неразборчивый скан\n123 456",
])
def test_mixed_quantity_tables_and_unknown_scans_remain_blocking(text):
    assert not deferred_drawing_errors(page(text))


def test_drawing_title_does_not_hide_a_different_technical_failure():
    assert not deferred_drawing_errors(page("Продольный профиль", "ocr_failed: file missing"))


def test_agent_coverage_separates_deferred_drawings_from_blocking_table_errors():
    index = SimpleNamespace(
        texts=["Продольный профиль", "Ведомость объемов работ\nНаименование Количество"],
        errors={0: "scan_table_failed:TimeoutExpired:90", 1: "scan_table_failed:TimeoutExpired:90"},
    )
    coverage = coverage_for_pages(index)
    assert coverage["error_pages"] == [2]
    assert coverage["technical_error_pages"] == [1, 2]
    assert [p["page"] for p in coverage["deferred_visual_pages"]] == [1]
    assert len(index.errors) == 2
