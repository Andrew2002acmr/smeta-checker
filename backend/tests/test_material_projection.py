"""Selective material replay projection; real PostgreSQL cases are opt-in."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from audit_models import VorPosition
from normative_parameters import enrich
from normative_layer_assembly import ALLOCATION_PROOF, verified_material_allocation
from normative_verdict import REVIEW, publish
from normative_workbook_rate import PROOF as AREA_RATE, project_rate_proof
from storage_db import Repository
from test_durable_tasks import pipeline as pipeline
from test_planned_allocation_integration import prepared

KINDS = {"claim-parser-corrections-v1", "workbook_arithmetic_proof_v1",
    "explicit_curb_column_quantity_v1", "parent_geometry_dependency_v1", ALLOCATION_PROOF, AREA_RATE,
    "single_base_price_application_v1"}


def material_data(kind):
    if kind == "allocation":
        positions, raw, relation = prepared()
        position = positions[1]
    elif kind == "geometry":
        from test_current_material_geometry import current_fixture, bind
        record, raw, positions, context = current_fixture()
        position = positions[1]
        raw = bind(record, raw, positions, context)
        raw["coverage"] = {"coefficient_policy": "independent_evidence_required"}
        relation = dict(parent={"index": 0})
    else:
        position = VorPosition(index=46, work_name="Растительная земля", position_type="Материал",
            volume="20.2", unit="м3", formula="101*20/100", comment="Расход20м3/100м2")
        positions, relation = {46: position}, {}
        area, volume = "Газон: площадь 101 м2", "Газон: объём 20.2 м3"
        raw = dict(pipeline_version=24, execution_state="complete", summary="Материал", items=[
            dict(topic="Количество", claim_ids=["quantity", "unit"], state="confirmed", required=True,
                project_source_ids=["volume"], normative_source_ids=[], explanation="Проектное количество"),
            dict(topic="Норма", claim_ids=["normative:applicability"], state="confirmed", required=True,
                project_source_ids=["volume"], normative_source_ids=["n"], explanation="Спецификация"),
            dict(topic="Расход", claim_ids=["derived:rate"], state="confirmed", required=True,
                project_source_ids=["area", "volume"], normative_source_ids=[], explanation="Физический расход")],
            sources=[dict(id="area", kind="project", origin="native", version_id="pdf", page=1, text=area),
                dict(id="volume", kind="project", origin="native", version_id="pdf", page=1, text=volume),
                dict(id="n", kind="normative", origin="native", text="Спецификация земли", label="Спецификация")],
            parameter_proposals=[dict(parameter="quantity", value="101", unit="м2", source_id="area",
                quote=area, scope_quote=area, scope_state="verified"),
                dict(parameter="quantity", value="20.2", unit="м3", source_id="volume",
                quote=volume, scope_quote=volume, scope_state="verified")])
    review = enrich(position, raw, positions, relation)
    review["position_key"] = "material-key"
    return position, review


def status(position, review):
    return publish({"Статус аудита": "ОК"}, {"checks": []}, "material-key",
        dict(stage="done", review=review), enabled=True, derived=position.index != 46, position=position)


def selected_traces(review):
    events = review.get("evidence_trace", [])
    selected = [e for e in events
        if isinstance(e.get("kind"), str) and e["kind"] in KINDS]
    return selected + ([e for e in events if e.get('kind')=='norm_thickness_adjustment_proposal_v1']
        if any(e['kind']=='single_base_price_application_v1' for e in selected) else [])


def offline_projection(review):
    """Run repository assembly and inspect real SQL without a database connection."""
    rid = uuid4()
    proofs = selected_traces(review)
    material = any(e["kind"] in {ALLOCATION_PROOF, AREA_RATE, "parent_geometry_dependency_v1",
        "single_base_price_application_v1"} for e in proofs)
    proposals = [p for p in review.get("parameter_proposals", [])
        if p.get("parameter") in {"quantity", "thickness", "coefficient", "unit_scale"}]
    rows = [(rid, 1, review["position_key"], review["items"], review.get("summary"), review["parameter_cards"],
        review.get("parameter_schema_version"), review.get("execution_state"), review.get("local_rules_version"),
        review.get("pipeline_version"), proofs, proposals if material else None,
        review.get("coverage", {}).get("coefficient_policy") if material else None)]
    session = Mock()
    session.scalars.return_value = []
    replies: list[Any] = [rows]
    if material:
        ids = Repository._material_replay_source_ids(review["items"], review["parameter_cards"], proofs, proposals)
        sources = [s for s in review["sources"] if s["id"] in ids and s["kind"] in {"project", "normative"}]
        replies.append([(rid, sources, proposals)])
    session.execute.side_effect = replies
    repository = SimpleNamespace(_engine=None, _material_replay_source_ids=Repository._material_replay_source_ids)
    with patch("storage_db.Session") as factory:
        factory.return_value.__enter__.return_value = session
        out = Repository.normative_validation_context(cast(Repository, repository), uuid4())[1]["review"]
    return out, [c.args[0].compile(dialect=postgresql.dialect()) for c in session.execute.call_args_list]


@pytest.mark.parametrize("kind", ["allocation", "area_rate", "geometry"])
def test_selective_repository_assembly_preserves_material_replay_and_excludes_noise(kind):
    position, review = material_data(kind)
    original = deepcopy(review)
    review["sources"].append(dict(id="unused", kind="project", origin="native", text="private-ocr-noise" * 1000))
    review["sources"].append(dict(id="unused-norm", kind="normative", text="unrelated-source-noise" * 1000))
    review["telemetry"] = {"private": "private-ocr-noise"}
    review["evidence_trace"].append(dict(kind="tool", output="private-ocr-noise"))
    before = deepcopy(review)
    projected, queries = offline_projection(review)
    assert review == before
    assert status(position, original)["Статус аудита"] == (REVIEW if kind == "geometry" else "ОК")
    assert status(position, projected) == status(position, review)
    assert projected["parameter_proposals"] == review["parameter_proposals"]
    assert "telemetry" not in projected
    assert "private-ocr-noise" not in json.dumps(projected, ensure_ascii=False)
    assert "unrelated-source-noise" not in json.dumps(projected, ensure_ascii=False)
    paths = " ".join(str(v) for query in queries for v in query.params.values())
    assert ALLOCATION_PROOF in paths and AREA_RATE in paths
    assert '@.kind.type() == "string"' in paths and '@.id == $ids[*]' in paths
    assert "coefficient" in paths and "unit_scale" in paths
    if kind == "allocation":
        assert projected["coverage"]["coefficient_policy"] == "estimator_value_normative_conditions"
        assert any(s["id"] == "sp" and s["kind"] == "normative" for s in projected["sources"])
        assert verified_material_allocation(projected, position)
    elif kind == "geometry":
        from normative_parent_basis import verified_geometry_dependency
        assert verified_geometry_dependency(projected, position)
        assert projected["coverage"]["coefficient_policy"] == "independent_evidence_required"
        assert any(s["id"] == "own" and s["kind"] == "normative" for s in projected["sources"])
        assert any(p["parameter"] == "coefficient" for p in projected["parameter_proposals"])
    else:
        assert project_rate_proof(position, projected)


def test_raw_parameter_dependencies_and_norm_anchors_are_selected_without_full_registry():
    items = [dict(project_source_ids=["p"], normative_source_ids=["sp"])]
    cards = [dict(source_anchors=[dict(source_id="card-anchor")])]
    proofs = [dict(root_source_ids=["root"], geometric_rate_basis=dict(source_sha256={"dependency": "hash"}))]
    proposals = [dict(parameter=k, source_id=k, operand_source_ids=["operand"])
        for k in ("quantity", "thickness", "coefficient", "unit_scale")]
    ids = Repository._material_replay_source_ids(items, cards, proofs, proposals)
    assert set(ids) == {"p", "sp", "card-anchor", "root", "dependency", "operand",
        "quantity", "thickness", "coefficient", "unit_scale"}


def test_work59_without_material_proof_retains_existing_small_projection():
    from test_normative_validation_projection import fixture
    data = fixture()
    review = {**data["position_check"], "position_key": data["position_key"]}
    projected, queries = offline_projection(review)
    assert len(queries) == 1
    assert not {"sources", "parameter_proposals", "coverage", "telemetry"} & projected.keys()
    assert all(e["kind"] not in {ALLOCATION_PROOF, AREA_RATE} for e in projected["evidence_trace"])


@pytest.fixture
def material_pipeline(request):
    if not os.environ.get("TEST_POSTGRES_URL"):
        pytest.skip("Set TEST_POSTGRES_URL to an isolated test database for PostgreSQL projection QA")
    return request.getfixturevalue("pipeline")


@pytest.mark.parametrize("kind", ["allocation", "area_rate", "geometry"])
@pytest.mark.parametrize("mutation", ["none", "source", "parameters", "coefficient_policy"])
def test_postgres_full_and_batch_material_verdicts_match_with_rechecked_proofs(material_pipeline, kind, mutation):
    p = material_pipeline
    position, review = material_data(kind)
    assert status(position, review)["Статус аудита"] == (REVIEW if kind == "geometry" else "ОК")
    if mutation == "source":
        next(s for s in review["sources"] if s["kind"] == "project")["text"] = "Другая конструкция999м3"
    elif mutation == "parameters":
        review["parameter_proposals"] = []
    elif mutation == "coefficient_policy":
        review.setdefault("coverage", {})["coefficient_policy"] = "independent_evidence_required"
    review["sources"].append(dict(id="unused", kind="project", text="private-ocr-noise" * 1000))
    review["sources"].append(dict(id="unused-norm", kind="normative", text="unrelated-source-noise" * 1000))
    review["evidence_trace"].extend([dict(kind="tool", output="private-ocr-noise"),
        dict(kind=[ALLOCATION_PROOF], output="not-a-typed-proof"),
        dict(kind="tool", nested=dict(kind=AREA_RATE, output="not-top-level"))])
    before = deepcopy(review)
    p.repo.save_normative_review(p.run.id, position.index, "check",
        dict(position_key="material-key", position_check=review), "material-projection")
    projected = p.repo.normative_validation_context(p.run.id)[position.index]["review"]
    assert status(position, projected) == status(position, review)
    if mutation in {"source", "parameters"}:
        assert status(position, projected)["Статус аудита"] != "ОК"
    if mutation == "coefficient_policy" and kind == "allocation":
        assert status(position, projected)["Статус аудита"] == REVIEW
    assert review == before
    assert {e["kind"] for e in projected["evidence_trace"]} <= KINDS
    assert "private-ocr-noise" not in json.dumps(projected, ensure_ascii=False)
    assert "unrelated-source-noise" not in json.dumps(projected, ensure_ascii=False)
    assert projected["parameter_proposals"] == review["parameter_proposals"]
    assert projected["coverage"] == ({"coefficient_policy":review["coverage"]["coefficient_policy"]}
        if review.get("coverage", {}).get("coefficient_policy") is not None else {})


def test_postgres_material_raw_parameter_types_are_preserved(material_pipeline):
    p = material_pipeline
    position, review = material_data("allocation")
    review["sources"].append(dict(id="extra", kind="normative", origin="native", text="Коэффициент2; Измеритель1000м2"))
    review["parameter_proposals"].extend([
        dict(parameter="coefficient", value="2", unit="", source_id="extra", quote="Коэффициент2"),
        dict(parameter="unit_scale", value="1000", unit="м2", source_id="extra", quote="Измеритель1000м2")])
    p.repo.save_normative_review(p.run.id, position.index, "check",
        dict(position_key="material-key", position_check=review), "material-parameter-projection")
    projected = p.repo.normative_validation_context(p.run.id)[position.index]["review"]
    assert {v["parameter"] for v in projected["parameter_proposals"]} == {"quantity", "thickness", "coefficient", "unit_scale"}
    assert next(s for s in projected["sources"] if s["id"] == "extra")["kind"] == "normative"
    assert projected["parameter_proposals"] == review["parameter_proposals"]


@pytest.mark.parametrize("mutation", ["source", "parameters", "scope", "proof", "duplicate", "quantity_card", "area_card", "arithmetic_card"])
def test_geometric_rate_raw_integrity_is_required_even_without_matching_rate_obligation(mutation):
    from normative_workbook_rate import geometric_rate_replay_supported
    position, review=material_data("area_rate")
    assert geometric_rate_replay_supported(position,review)
    if mutation=="source":
        next(s for s in review["sources"] if s["id"]=="area")["text"]="Другая конструкция 999 м2"
    elif mutation=="parameters":
        review["parameter_proposals"]=[]
    elif mutation=="scope":
        next(p for p in review["parameter_proposals"] if p["source_id"]=="area")["scope_state"]="unresolved"
    elif mutation=="proof":
        next(e for e in review["evidence_trace"] if e.get("kind")==AREA_RATE)["geometric_rate_basis"]=None
    elif mutation=="duplicate":
        review["evidence_trace"].append(deepcopy(next(e for e in review["evidence_trace"] if e.get("kind")==AREA_RATE)))
    elif mutation in {"quantity_card", "area_card"}:
        key="quantity" if mutation=="quantity_card" else "input:area"
        next(c for c in review["parameter_cards"] if c["id"]==key)["found_value"]="999"
    else:
        review["parameter_cards"]=[c for c in review["parameter_cards"] if c["id"]!="calculation:46"]
    assert not geometric_rate_replay_supported(position,review)
    projected,_=offline_projection(review)
    assert status(position,review)["Статус аудита"]==status(position,projected)["Статус аудита"]==REVIEW


def test_interpretation_only_area_rate_trace_does_not_claim_certified_project_geometry():
    from normative_workbook_rate import geometric_rate_replay_supported
    assert geometric_rate_replay_supported(None,dict(evidence_trace=[dict(kind=AREA_RATE,rate="0.2")]))
