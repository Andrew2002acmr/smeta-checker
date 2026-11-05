from types import SimpleNamespace
from unittest.mock import patch

import pytest
from normative_agent import EvidenceRequest, EvidenceTools
from normative_multidoc import attach, route
from pdf_binding import resolve_position_pdfs


class Tools:
    def __init__(self):
        self.version = "main"
        self.filename = "main.pdf"
        self.blob_store = object()
        self.record = {
            "config": {"pdf_versions": {"main.pdf": "main", "pos.pdf": "pos"}}
        }
        self.sources = []
        self.cells = {}
        self.coverage = {}
        self.events = []
        self.preferred = []
        self.table_search_done = False

    def _execute_single(self, request):
        return {
            "pages": [{"page": request.page, "document_version_id": self.version}],
            "search": {},
            "repeated_query": False,
        }


def prepared(version="pos", pending=()):
    return SimpleNamespace(
        pending_ocr=pending,
        metadata={"preparation_complete": True, "document_version_id": version},
        texts=["POS facts"],
        errors={},
    )


def setup():
    t = Tools()
    with patch("audit_snapshot.load_snapshot", return_value=prepared()):
        attach(
            t,
            {
                "supporting_documents": {
                    "pos.pdf": {"version_id": "pos", "snapshot": {"key": "frozen"}}
                }
            },
        )
    return t


def test_multiple_binding_is_explicit_opt_in_and_keeps_missing_errors():
    assert resolve_position_pdfs(["a.pdf", "b.pdf"], ["a.pdf", "b.pdf"])[3]
    assert resolve_position_pdfs(
        ["a.pdf", "b.pdf"], ["a.pdf", "b.pdf"], allow_multiple=True
    ) == (["a.pdf", "b.pdf"], [], [], None)
    assert resolve_position_pdfs(["a.pdf", "b.pdf"], ["a.pdf"], allow_multiple=True)[3]


def test_position_preparation_retains_supporting_names_without_changing_primary():
    from audit_models import VorPosition
    from audit_tasks import _prepare_one_position
    from unittest.mock import Mock

    repo = Mock()
    repo.create_position_job.return_value = SimpleNamespace(status="pending")
    p = VorPosition(
        index=0,
        work_name="Work",
        volume="1",
        unit="м2",
        explicit_refs=["a.pdf", "b.pdf"],
    )
    with patch.dict("os.environ", {"AUDIT_MULTI_PDF_CONTEXT": "1"}):
        result = _prepare_one_position(
            context=SimpleNamespace(repository=repo),
            run_id="run",
            worker="test",
            vor_name="book.xlsx",
            position=p,
            meta={},
            pool_keys=["a.pdf", "b.pdf"],
            pdf_versions={},
        )
    assert result["pdf_name"] == "a.pdf"
    assert result["supporting_pdf_names"] == ["b.pdf"]
    assert result["bind_error"] is None


@pytest.mark.parametrize("index", [prepared("wrong"), prepared(pending=[0])])
def test_unpinned_or_incomplete_supporting_document_is_rejected(index):
    with (
        patch("audit_snapshot.load_snapshot", return_value=index),
        pytest.raises(ValueError),
    ):
        attach(
            Tools(),
            {
                "supporting_documents": {
                    "pos.pdf": {"version_id": "pos", "snapshot": {}}
                }
            },
        )


def test_search_both_documents_and_read_original_page_namespace():
    t = setup()
    result = route(t, EvidenceRequest(tool="search_tables", query="embankment"))
    assert {p["document_version_id"] for p in result["pages"]} == {"main", "pos"}
    assert t.table_search_done
    read = route(
        t, EvidenceRequest(tool="read_page", page=1, document_version_id="pos")
    )
    assert read["pages"] == [{"page": 1, "document_version_id": "pos"}]
    assert route(t, EvidenceRequest(tool="read_page", page=1)) is None
    assert (
        route(t, EvidenceRequest(tool="read_page", document_version_id="unknown"))[
            "error"
        ]
        == "unknown_document_version"
    )
    assert t.additional_documents["pos"].sources is t.sources


def test_same_physical_page_number_cannot_merge_different_pdfs():
    t = setup()
    t.index = prepared("main")
    t._tables = lambda p: []
    child = t.additional_documents["pos"]
    child._tables = lambda p: []
    for obj in (t, child):
        obj.index.quality = {}
        obj._add = lambda source: {"source_id": source["id"]}
    a = EvidenceTools._page(t, 1, 0)
    b = EvidenceTools._page(child, 1, 0)
    assert a["source_id"] != b["source_id"]
    assert a["document_version_id"] == "main" and b["document_version_id"] == "pos"


def test_sums_never_mix_document_namespaces_even_with_explicit_target():
    t = setup()
    t.sources.extend(
        [{"id": "a", "version_id": "main"}, {"id": "b", "version_id": "pos"}]
    )
    request = EvidenceRequest(tool="sum_cells", operand_ids=["a", "b"], unit="м2")
    assert "cross_document" in route(t, request)["error"]
    assert "cross_document" in EvidenceTools._sum(t, request)["error"]
