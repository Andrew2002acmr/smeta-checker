"""Document barrier, durable page resume and shared immutable evidence; no LLM quota."""

from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

from audit_tasks import PreparationYield, prepare_run
from audit_snapshot import load_snapshot
from pdf_text import PDFTextSource
from test_tasks import TaskPipelineTests as _TaskPipelineTests, _fake_vor_bytes


def pdf_bytes(label="A", page_count=3):
    with pymupdf.open() as doc:
        for page in range(page_count):
            p = doc.new_page()
            p.insert_text((40, 60), (f"{label} page {page}: Copper cable 45 m. ") * 4)
        return doc.tobytes()


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context, repo = _TaskPipelineTests()._make_context(str(tmp_path / "blobs"))

    def make(*, multiple=False, page_count=3):
        project = repo.create_project("document barrier")
        vor_bytes = _fake_vor_bytes(
            ("1;Copper cable;45;m;;project.pdf;1", "2;Copper cable;45;m;;project.pdf;1")
        )
        vor = repo.add_document_version(
            project.id, vor_bytes, filename="vor.csv", kind="vor", new_document=True
        )
        files = {"project.pdf": pdf_bytes(page_count=page_count)}
        if multiple:
            files["second.pdf"] = pdf_bytes("B")
        versions = {
            name: repo.add_document_version(
                project.id, data, filename=name, kind="pdf", new_document=True
            )
            for name, data in files.items()
        }
        run = repo.create_run(
            project.id,
            vor.id,
            {"pdf_versions": {name: str(v.id) for name, v in versions.items()}},
            pdf_pins={name: v.id for name, v in versions.items()},
        )
        return SimpleNamespace(
            context=context,
            repo=repo,
            run=run,
            vor=vor_bytes,
            files=files,
            versions=versions,
        )

    yield make
    repo._engine.dispose()


def prepare(p, **kwargs):
    return prepare_run(
        p.context,
        p.run.id,
        project="test",
        vor_filename="vor.csv",
        vor_bytes=p.vor,
        pdf_files=p.files,
        pdf_versions=p.versions,
        worker="test-pdf",
        slice_seconds=120,
        **kwargs,
    )


def test_resume_does_not_repeat_pages_or_native_pass_and_queues_shared_corpus(build):
    p = build()
    real = PDFTextSource.prepare_page
    visited = []

    def interrupted(source, page):
        if page == 1:
            raise PreparationYield()
        return real(source, page)

    dispatched = []
    with patch.object(PDFTextSource, "prepare_page", interrupted):
        assert prepare(p, dispatch=lambda *args: dispatched.append(args))["continue"]
    assert not dispatched
    assert p.repo.list_position_jobs(p.run.id) == []
    config = p.repo.load_run(p.run.id)["config"]
    assert config["pdf_preparation"] == {"done": 1, "total": 3}
    assert not config.get("documents_prepared")

    def resumed(source, page):
        visited.append(page)
        return real(source, page)

    def dispatch(name, payload):
        config = p.repo.load_run(p.run.id)["config"]
        assert config["documents_prepared"]
        assert config["pdf_preparation"] == {"done": 3, "total": 3}
        dispatched.append(payload)

    with (
        patch.object(
            PDFTextSource,
            "_read_native",
            side_effect=AssertionError("repeated native pass"),
        ),
        patch.object(
            PDFTextSource, "for_position", side_effect=AssertionError("position OCR")
        ),
        patch.object(PDFTextSource, "prepare_page", resumed),
    ):
        assert prepare(p, dispatch=dispatch)["queued"] == [0, 1]
    assert visited == [1, 2]
    jobs = p.repo.list_position_jobs(p.run.id)
    assert jobs[0].payload["snapshot"] == jobs[1].payload["snapshot"]
    index = load_snapshot(p.context.blob_store, jobs[0].payload["snapshot"])
    assert len(index.texts) == 3 and not index.pending_ocr
    assert index.metadata["preparation_complete"]
    with patch.object(
        PDFTextSource,
        "__init__",
        side_effect=AssertionError("reopened completed document"),
    ):
        assert prepare(p)["queued"] == [0, 1]


def test_no_position_dispatched_until_all_selected_pdfs_are_complete(build):
    p = build(multiple=True)
    real = PDFTextSource.prepare_page
    calls = 0

    def interrupted(source, page):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise PreparationYield()
        return real(source, page)

    with patch.object(PDFTextSource, "prepare_page", interrupted):
        assert prepare(p)["continue"]
    assert not p.repo.list_position_jobs(p.run.id)
    state = p.repo.load_run(p.run.id)["config"]
    assert state["prepared_documents"]["project.pdf"]["snapshot"]
    assert state["pdf_preparation"] == {"done": 3, "total": 6}
    assert not state.get("documents_prepared")
    assert prepare(p)["queued"] == [0, 1]


def test_full_pass_recognizes_unrelated_late_scans_and_preserves_failed_pages(build):
    p = build(page_count=7)
    recognized = []

    def native(source):
        return (("Copper cable 45 m",) + ("",) * 6, {}, list(range(1, 7)), set())

    def ocr(source, page):
        recognized.append(page)
        if page == 6:
            raise RuntimeError("OCR unavailable for this page")
        return dict(
            text="Unrelated quantity schedule 345.77 m2",
            words=[],
            quality=dict(state="usable", rotation=0),
        )

    def table(source, page):
        source._store_tables(
            page,
            [
                dict(
                    table_id="late",
                    row_count=2,
                    col_count=1,
                    cells=[
                        dict(
                            cell_id="h",
                            row=0,
                            column=0,
                            text="Area m2",
                            role="header",
                            header_ids=[],
                            bbox=[],
                            origin="ocr",
                        ),
                        dict(
                            cell_id="v",
                            row=1,
                            column=0,
                            text="345.77",
                            role="cell",
                            header_ids=["h"],
                            bbox=[],
                            origin="ocr",
                        ),
                    ],
                )
            ],
            origin="ocr",
        )

    with (
        patch.object(PDFTextSource, "_read_native", native),
        patch.object(PDFTextSource, "_ocr_page_result", ocr),
        patch.object(PDFTextSource, "_scan_grid_for", table),
    ):
        assert prepare(p)["queued"] == [0, 1]
    assert recognized == list(range(1, 7))
    index = load_snapshot(
        p.context.blob_store, p.repo.list_position_jobs(p.run.id)[0].payload["snapshot"]
    )
    assert "345.77" in index.texts[1]
    assert index.tables[5][0]["cells"][1]["text"] == "345.77"
    assert index.locators[5][0]["page"] == 6
    assert not index.pending_ocr
    assert index.quality[6]["state"] == "failed"
    assert 6 in index.errors
    assert index.metadata["failed_pages"] == [7]
    assert index.metadata["preparation_state"] == "complete_with_errors"


def test_mixed_page_with_native_text_and_raster_content_also_gets_ocr(tmp_path):
    import io
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_textbox(pymupdf.Rect(40, 40, 550, 95), "Good native paragraph with many readable words. " * 6)
        p.insert_image(pymupdf.Rect(40, 100, 400, 400), stream=image.getvalue())
        with (
            patch.object(
                PDFTextSource,
                "_ocr_page_result",
                return_value=dict(
                    text="Embedded raster schedule 12.5 m2",
                    words=[],
                    quality=dict(state="usable"),
                ),
            ) as ocr,
            patch.object(PDFTextSource, "_scan_grid_for"),
        ):
            source = PDFTextSource(doc, cache_dir=tmp_path / "pdf")
            try:
                assert not source.pending_ocr
                packed = source.prepare_page(0)
            finally:
                source.close()
        assert ocr.call_count == 1
        assert "Embedded raster schedule" in packed["text"]


def test_llm_worker_refuses_partial_document_without_reopening_or_calling_model(build):
    from audit_core import PageTextIndex
    from audit_snapshot import store_snapshot
    from audit_tasks import audit_position

    p = build()
    prepare(p)
    job = p.repo.list_position_jobs(p.run.id)[0]
    payload = dict(job.payload)
    payload["snapshot"] = store_snapshot(
        p.context.blob_store,
        PageTextIndex(
            ("Copper cable 45 m",),
            pending_ocr=(0,),
            metadata={"preparation_complete": False},
        ),
    )
    p.repo.requeue_position_job(p.run.id, 0, queue="llm", payload=payload)
    with patch("pymupdf.open", side_effect=AssertionError("worker opened PDF")):
        audit_position(p.context, p.run.id, 0, worker="test-llm")
    result = p.repo.load_position_job(p.run.id, 0).result
    assert result["status"] == "Технический сбой"
    assert result.get("requests", 0) == 0
    assert "подготовка ПД" in result["discrepancy_details"]
