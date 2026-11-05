import importlib.util
from pathlib import Path


def test_drawing_routing_change_invalidates_normative_answers_without_invalidating_matching(tmp_path):
    source = Path(__file__).resolve().parents[1] / "analysis_replay.py"
    copy = tmp_path / "analysis_replay.py"
    copy.write_bytes(source.read_bytes())
    route = tmp_path / "pdf_page_routing.py"
    route.write_text("VERSION = 'first'\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("isolated_replay_dependency", copy)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    normative = module.algorithm("normative")
    matching = module.algorithm("matching")
    route.write_text("VERSION = 'changed'\n", encoding="utf-8")
    module.algorithm.cache_clear()
    assert module.algorithm("normative") != normative
    assert module.algorithm("matching") == matching


def test_grid_recovery_change_invalidates_the_table_cache(monkeypatch):
    from pdf_text import PDFTextSource,MAX_TABLE_CELL_CHARS
    import pdf_ruled_recovery
    before=PDFTextSource._table_key_for(MAX_TABLE_CELL_CHARS)
    monkeypatch.setattr(pdf_ruled_recovery,'VERSION',pdf_ruled_recovery.VERSION+'-changed')
    assert PDFTextSource._table_key_for(MAX_TABLE_CELL_CHARS)!=before
