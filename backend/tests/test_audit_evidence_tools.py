"""Graph tools read pinned prepared corpora only; bundles resume deterministically."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from audit_evidence_tools import (
    EvidenceRequest,
    attach_supporting_tools,
    load_prepared_tools,
    restore_tool_bundle,
    tool_bundle,
)
from audit_evidence_validation import (
    AgentAnswer,
    CheckAnswer,
    answer_feedback,
    bind_explicit_sum_sources,
    canonicalize_cell_parameters,
    normalize_rounded_sum,
    validate_items,
    validated_items,
)


class MemoryBlobs:
    def __init__(self):
        self.snapshots = {}

    def register(self, key, index):
        self.snapshots[key] = index

    def load(self, _blob_store, ref):
        return self.snapshots[ref["key"]]


def prepared_index(version, *, pages=1):
    return SimpleNamespace(
        texts=("Площадь нижнего основания 431,611 м2",)[:pages],
        tables=([[] for _ in range(pages)]),
        errors={},
        pending_ocr=[],
        quality={},
        metadata={"document_version_id": version, "preparation_complete": True},
    )


def test_prepared_loader_rejects_missing_or_mismatched_corpus(monkeypatch):
    blobs = MemoryBlobs()
    blobs.register("primary", prepared_index("pdf-1"))
    monkeypatch.setattr("audit_snapshot.load_snapshot", blobs.load)
    with pytest.raises(ValueError, match="pinned version"):
        load_prepared_tools(blob_store=None, snapshot_refs=[], version_id="pdf-1")
    with pytest.raises(ValueError, match="another document"):
        load_prepared_tools(blob_store=None, snapshot_refs=[{"key": "primary"}], version_id="pdf-2")
    tools = load_prepared_tools(blob_store=None, snapshot_refs=[{"key": "primary"}], version_id="pdf-1")
    assert tools.snapshot_refs == []
    assert tools.execute(EvidenceRequest(tool="read_page", page=1))["page"] == 1


def test_tool_bundle_round_trip_preserves_registry_and_cursors(monkeypatch):
    blobs = MemoryBlobs()
    blobs.register("primary", prepared_index("pdf-1"))
    monkeypatch.setattr("audit_snapshot.load_snapshot", blobs.load)
    tools = load_prepared_tools(blob_store=None, snapshot_refs=[{"key": "primary"}], version_id="pdf-1")
    first = tools.execute(EvidenceRequest(tool="read_page", page=1))
    bundle = tool_bundle(tools)
    restored = restore_tool_bundle(
        blob_store=None, bundle=bundle, snapshot_refs=[{"key": "primary"}]
    )
    assert [source["id"] for source in restored.sources] == [source["id"] for source in tools.sources]
    assert restored.execute(EvidenceRequest(tool="read_source", source_id=first["source_id"]))["source_id"] == first["source_id"]
    assert restored.snapshot_refs == []
    with pytest.raises(ValueError, match="bundle"):
        restore_tool_bundle(blob_store=None, bundle={**bundle, "version": 999}, snapshot_refs=[{"key": "primary"}])


def test_supporting_documents_require_explicit_pins(monkeypatch):
    blobs = MemoryBlobs()
    blobs.register("primary", prepared_index("pdf-1"))
    blobs.register("support", prepared_index("pdf-2"))
    monkeypatch.setattr("audit_snapshot.load_snapshot", blobs.load)
    tools = load_prepared_tools(blob_store=None, snapshot_refs=[{"key": "primary"}], version_id="pdf-1")
    attach_supporting_tools(
        tools, blob_store=None, documents={"pdf-2": {"snapshot": {"key": "support"}, "filename": "support.pdf"}}
    )
    assert tools.execute(EvidenceRequest(tool="read_page", page=1, document_version_id="pdf-9")) == {
        "error": "unknown_document_version"
    }
    with pytest.raises(ValueError, match="duplicates"):
        attach_supporting_tools(
            tools, blob_store=None, documents={"pdf-1": {"snapshot": {"key": "primary"}}}
        )


def test_heavy_pdf_ocr_imports_stay_out_of_graph_tool_modules():
    script = (
        "import sys; import audit_evidence_tools, audit_evidence_validation; "
        "heavy={'pymupdf','pandas','openpyxl','PIL','onnxruntime','cv2','rapidocr'}; "
        "found=sorted(heavy & set(sys.modules)); "
        "assert not found, found"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_extracted_validators_keep_legacy_addresses_and_repairs():
    sources = [
        {"id": "project:p1:0", "kind": "project", "page": 1, "text": "Площадь 431,611 м2", "origin": "native"},
        {"id": "n1", "kind": "normative", "page": 2, "text": "Условие нормы", "origin": "native"},
    ]
    answer = AgentAnswer(
        summary="Проверка",
        items=[
            {
                "topic": "Количество",
                "state": "confirmed",
                "explanation": "Площадь подтверждена.",
                "claim_ids": ["quantity"],
                "project_source_ids": ["project:p1:0"],
                "normative_source_ids": [],
            }
        ],
        parameters=[],
    )
    assert validated_items(answer, sources, known_claims=["quantity"])[0]["state"] == "confirmed"
    assert validate_items(answer.items, sources, known_claims=["quantity"])[0]["state"] == "confirmed"
    assert CheckAnswer(summary="Проверка", items=[], parameters=[]).summary == "Проверка"
    assert callable(answer_feedback)
    assert callable(bind_explicit_sum_sources)
    assert callable(canonicalize_cell_parameters)
    assert callable(normalize_rounded_sum)

def test_prepared_quantity_candidates_find_total_without_vor_amount(monkeypatch):
    from test_normative_agent import toolkit
    from test_normative_table_candidates import table
    from audit_evidence_tools import prepared_quantity_candidates

    tools = toolkit()
    value = table()
    tools.index.tables = ([value], [], [])
    position = {"index": 0, "work_name": "Материал Б", "unit": "м2",
                "position_type": "Материал", "position_source": {"sections": []}}
    found = prepared_quantity_candidates(tools, position, {"position_inputs": [position]})
    assert found
    assert found[0]["calculated_sum"] == "22"
    assert found[0]["discovery_only"] is True
    assert found[0]["scope_verified"] is False


def test_prepared_quantity_candidates_use_following_material_fraction(monkeypatch):
    from test_normative_agent import toolkit
    from test_normative_table_candidates import table
    from audit_evidence_tools import prepared_quantity_candidates
    from normative_agent import EvidenceTools

    tools = toolkit()
    tools.index.tables = ([table()], [], [])
    work = {"index": 0, "work_name": "Устройство слоя", "unit": "м2",
            "position_type": "Работа",
            "position_source": {"sections": [{"text": "Раздел 1"}]}}
    material = {"index": 1, "work_name": "Щебеночно-песчаная смесь 0/63", "unit": "м3",
                "position_type": "Материал",
                "position_source": {"sections": [{"text": "Раздел 1"}]}}
    queries = []
    original = EvidenceTools.quantity_candidates

    def spy(self, query, **kwargs):
        queries.append(query)
        return original(self, query, **kwargs)

    monkeypatch.setattr(EvidenceTools, "quantity_candidates", spy)
    prepared_quantity_candidates(tools, work, {"position_inputs": [work, material]})
    assert queries and "0/63" in queries[0]


def test_prepared_quantity_candidates_offer_area_basis_for_mass_rows():
    from test_normative_agent import toolkit
    from test_normative_table_candidates import table
    from audit_evidence_tools import prepared_quantity_candidates

    tools = toolkit()
    tools.index.tables = ([table()], [], [])
    position = {"index": 0, "work_name": "Материал Б", "unit": "т",
                "position_type": "Материал", "position_source": {"sections": []}}
    found = prepared_quantity_candidates(tools, position, {"position_inputs": [position]})
    assert found

def test_merge_symmetric_axes_offers_precomputed_sum():
    from types import SimpleNamespace

    from audit_evidence_tools import _merge_symmetric_axes

    left = {"page": 89, "row": 41, "table_id": "t", "operation": "direct",
            "numeric_verified": True, "columns": [16], "cell_ids": ["c16"],
            "operand_source_ids": ["s16"], "values": ["345,77"],
            "headers": ["основание", "щебеночно-песчаная смесь фр. 0/31,5", "h=0,24м", "площадь,"]}
    right = {"page": 89, "row": 41, "table_id": "t", "operation": "direct",
             "numeric_verified": True, "columns": [22], "cell_ids": ["c22"],
             "operand_source_ids": ["s22"], "values": ["85,841"],
             "headers": ["щебеночно-песчаная смесь фр. 0/31,5", "h=0,24м", "площадь,"]}
    tools = SimpleNamespace(execute=lambda request: {"source_id": "agent:sum:test"})
    merged = _merge_symmetric_axes([left, right], tools, "м2")
    total = next(item for item in merged if item.get("operation") == "sum")
    assert total["calculated_sum"] == "431.611"
    assert total["operand_source_ids"] == ["s16", "s22"]
    assert total["source_id"] == "agent:sum:test"
    assert total["discovery_only"] is True
