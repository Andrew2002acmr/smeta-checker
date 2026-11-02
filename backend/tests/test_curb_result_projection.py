from normative_curb_quantity import project
from normative_parameters import enrich
from normative_verdict import finalize_state, verified_quantity
from storage_blobs import FileBlobStore
from storage_db import open_repository
from test_storage import _postgres_url
from test_normative_curb_quantity import setup


def test_compact_database_projection_matches_full_curb_review_and_quantity(tmp_path):
    position, result, tools, relation = setup()
    assert project(position, result, tools, relation)
    result = enrich(position, result, {28: position}, relation)
    result["evidence_trace"].append(
        dict(kind="unneeded_tool_snapshot", large="not-for-result-list")
    )
    repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp_path))
    try:
        project_row = repo.create_project("Scoped quantity projection integration")
        vor = repo.add_document_version(
            project_row.id,
            b"isolated workbook",
            filename="vor.xlsx",
            kind="vor",
            new_document=True,
        )
        run = repo.create_run(project_row.id, vor.id, {})
        repo.save_normative_review(
            run.id,
            28,
            "check",
            dict(position_key="same", position_check=result),
            "test",
        )
        compact = repo.normative_validation_context(run.id)[28]["review"]
        stored = dict(status="Требуется ручная проверка")
        assert (
            finalize_state(result, stored, "same", position=position)["state"]
            == "possible_mismatch"
        )
        assert (
            finalize_state(compact, stored, "same", position=position)["state"]
            == "possible_mismatch"
        )
        assert verified_quantity(compact, "same", "м", position=position) == (
            "1574.00",
            "м",
        )
        assert all(s["kind"] == "project" for s in compact["sources"])
        assert all(p["parameter"] == "quantity" for p in compact["parameter_proposals"])
        assert not any(
            e.get("kind") == "unneeded_tool_snapshot" for e in compact["evidence_trace"]
        )
    finally:
        repo._engine.dispose()
