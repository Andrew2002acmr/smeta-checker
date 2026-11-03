from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4
import hashlib

import pymupdf

import pytest

from normative_benchmark_preflight import inspect, pdf_identity


def setup():
    version = str(uuid4())
    config = dict(
        pdf_versions={"pd.pdf": version},
        document_preparation_version=1,
        position_inputs=[
            dict(index=i, work_name="Работа", unit="м2", volume="10") for i in range(2)
        ],
        normative_corpora={version: dict(snapshot_key="canonical")},
    )
    origin: dict[str, Any] = dict(
        id=uuid4(),
        project_id=uuid4(),
        vor_version_id=uuid4(),
        status="done",
        config=config,
        indexed_results=[
            dict(position_index=i, result={"Статус аудита": "ОК"}) for i in range(2)
        ],
    )
    prepared = {**deepcopy(origin), "id": uuid4()}
    repo = Mock()
    repo.load_run.side_effect = [origin, prepared]
    repo.load_position_job.return_value = SimpleNamespace(
        payload=dict(pdf_name="pd.pdf", snapshot={"snapshot_key": "matching"})
    )
    with pymupdf.open() as pdf:
        pdf.new_page()
        raw=pdf.tobytes()
    blob_store=SimpleNamespace(get=Mock(return_value=raw))
    repo.load_version.return_value = SimpleNamespace(kind='pdf',sha256=hashlib.sha256(raw).hexdigest(),blob_key='raw.pdf')
    expected=pdf_identity(blob_store,repo.load_version.return_value)['extraction_sha256']
    snapshot = SimpleNamespace(
        metadata=dict(pdf_sha256=expected, preparation_complete=True),
        pending_ocr=[],
        errors={},
        texts=["page"],
        tables=[[{"table_id": "t"}]],
    )
    return (
        SimpleNamespace(repository=repo, blob_store=blob_store),
        origin,
        prepared,
        snapshot,
    )


def test_read_only_preflight_keeps_matching_and_preparation_separate_and_pinned():
    context, origin, prepared, snapshot = setup()
    before = deepcopy((origin, prepared))
    with patch(
        "normative_benchmark_preflight.load_snapshot", return_value=snapshot
    ) as load:
        report = inspect(context, origin["id"], prepared["id"], expected_count=2)
    assert (origin, prepared) == before
    assert (
        load.call_count == 2
    )  # Shared documents/snapshots, not one reload per position.
    assert len(report["jobs"]) == 2 and report["documents"][0]["tables"] == 1
    assert (
        report["model_requests"]
        == report["ocr_requests"]
        == report["database_writes"]
        == 0
    )
    assert (
        not report["performance_verified"]
        and report["confirmed_positions_not_measured"]
    )
    assert {call[0] for call in context.repository.method_calls} <= {
        "load_run",
        "load_position_job",
        "load_version",
    }


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "vor",
        "pdf_pins",
        "positions",
        "missing_matching",
        "duplicate_matching",
        "duplicate_position",
        "missing_preparation",
        "wrong_snapshot_pdf",
        "wrong_physical_pdf",
        "pending_ocr",
        "preparation_incomplete",
        "error_page",
        "no_snapshot",
    ],
)
def test_invalid_or_incomplete_inputs_cannot_be_certified(change):
    context, origin, prepared, snapshot = setup()
    if change == "active":
        origin["status"] = "running"
    elif change == "vor":
        prepared["vor_version_id"] = uuid4()
    elif change == "pdf_pins":
        prepared["config"]["pdf_versions"] = {"pd.pdf": str(uuid4())}
    elif change == "positions":
        prepared["config"]["position_inputs"][0]["volume"] = "999"
    elif change == "missing_matching":
        origin["indexed_results"].pop()
    elif change == "duplicate_matching":
        origin["indexed_results"].append(deepcopy(origin["indexed_results"][0]))
    elif change == "duplicate_position":
        origin["config"]["position_inputs"][1]["index"] = 0
        prepared["config"]["position_inputs"][1]["index"] = 0
    elif change == "missing_preparation":
        prepared["config"]["normative_corpora"] = {}
    elif change == "wrong_snapshot_pdf":
        snapshot.metadata["pdf_sha256"] = "another-pdf"
    elif change == "wrong_physical_pdf":
        context.repository.load_version.return_value.sha256 = "another-pdf"
    elif change == "pending_ocr":
        snapshot.pending_ocr = [0]
    elif change == "preparation_incomplete":
        snapshot.metadata["preparation_complete"] = False
    elif change == "error_page":
        snapshot.errors = {0: "failed"}
    elif change == "no_snapshot":
        context.repository.load_position_job.return_value.payload.pop("snapshot")
    with (
        patch("normative_benchmark_preflight.load_snapshot", return_value=snapshot),
        pytest.raises(ValueError),
    ):
        inspect(context, origin["id"], prepared["id"], expected_count=2)
