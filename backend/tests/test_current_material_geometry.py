"""Current literal inputs can prove work geometry without a parent model call."""
from copy import deepcopy
from typing import Any, cast

from audit_models import SourceHeading

import pytest

from normative_parent_basis import DEPENDENCY_TRACE, bind_parent_basis, verified_geometry_dependency
from normative_parameters import enrich
from test_normative_parent_geometry import fixture


def current_fixture() -> tuple[dict, dict, dict, Any]:
    donor, record, result, positions, context = cast(Any, fixture())
    section = [SourceHeading(address="A2", text="Раздел 1. Основание")]
    for index, position in positions.items():
        source = position.position_source.model_copy(update={"document_sha256": "a" * 64,
            "row": 10 + index, "sections": section})
        positions[index] = position.model_copy(update={"position_source": source})
    basis = donor["payload"]["position_check"]
    result["sources"] += deepcopy(basis["sources"])
    result["parameter_proposals"] += deepcopy(basis["parameter_proposals"])
    result["parameter_cards"] = []
    context.repository.normative_reviews.return_value = []
    return record, result, positions, context


def bind(record, result, positions, context, child=None):
    return bind_parent_basis(context, record, result, positions,
        dict(parent={"index": 0}, children=[]), position=child or positions[1])


def test_current_literal_geometry_is_rebuilt_without_parent_review_or_cards():
    record, result, positions, context = current_fixture()
    before = deepcopy(result)
    output = bind(record, result, positions, context)
    checked = enrich(positions[1], output, positions, dict(parent={"index": 0}))
    assert verified_geometry_dependency(checked, positions[1])
    assert result == before
    proof = next(e for e in output["evidence_trace"] if e["kind"] == DEPENDENCY_TRACE)
    assert proof["basis"] == "current_material_literal_geometry"
    assert "review_id" not in proof
    assert any(i["state"] == "needs_facts" and "derived:rate" in i["claim_ids"] for i in checked["items"])
    assert any(i["state"] == "needs_facts" and "normative:applicability" in i["claim_ids"] for i in checked["items"])
    assert not any(i["normative_source_ids"] for i in output["items"] if i["topic"].startswith("Подтверждённый параметр"))


def test_full_saved_replay_binds_geometry_after_parameter_envelope_repair(monkeypatch):
    from types import SimpleNamespace
    import normative_position_check as npc
    import normative_service as service

    record, result, positions, context = current_fixture()
    positions[0] = positions[0].model_copy(update={'work_name':
        'Устройство нижнего слоя основания из щебёночно-песчаной смеси, h=10 см'})
    positions[1] = positions[1].model_copy(update={'unit': 'м3', 'formula': '10*0.2',
        'calculation': dict(status='calculated', value='2', expression='10*0.2')})
    position = positions[1]
    record['config']['position_inputs'] = [p.model_dump(mode='json') for p in positions.values()]
    record['indexed_results'].append(dict(position_index=position.index, result=dict(status='needs_review', checks=[])))
    kb = SimpleNamespace(identity={'release': 'kb'})
    monkeypatch.setattr(service, 'corpus', lambda **kwargs: kb)
    monkeypatch.setattr(service, 'profile_for', lambda record: 'roads')
    context.repository.supports_analysis_replay = False
    context.repository.save_normative_review.side_effect = lambda run, index, kind, payload, actor: dict(payload=payload)
    row = dict(id='raw-review', payload=dict(position_key=service.position_key(position),
        vor_version_id=record['vor_version_id'], kb=kb.identity,
        work_relations=dict(parent={'index': 0}), position_check=result))
    before = deepcopy(row)
    saved = npc.validate_agent_saved(context, record, position.index, row, 'local', register=False)
    checked = saved['payload']['position_check']
    assert verified_geometry_dependency(checked, position)
    assert row == before
    # It proves geometric inputs only; the original material semantic unknowns remain.
    assert any(i['state']=='needs_facts' and 'derived:rate' in i['claim_ids'] for i in checked['items'])


@pytest.mark.parametrize("change", ["document", "section", "sheet", "owner", "pin", "empty", "unit", "scope", "quality", "child_output", "child_identity"])
def test_current_geometry_rejects_unbound_or_unproved_inputs(change):
    record, result, positions, context = current_fixture()
    child = positions[1]
    if change in {"document", "section", "sheet"}:
        field, value = {"document": ("document_sha256", "b" * 64),
            "section": ("sections", [SourceHeading(address="A20", text="Другой раздел")]),
            "sheet": ("sheet", "Other")}[change]
        positions[1] = child.model_copy(update={"position_source": child.position_source.model_copy(update={field: value})})
        child = positions[1]
    elif change == "owner":
        positions[2] = positions[0].model_copy(update={"index": 2,
            "position_source": positions[0].position_source.model_copy(update={"row": 10})})
    elif change == "pin":
        record["config"]["pdf_versions"] = {"PD": "another"}
    elif change == "empty":
        result["parameter_proposals"] = []
        result["parameter_cards"] = [dict(id="quantity", state="confirmed", found_value="10", found_unit="м2")]
    elif change == "quality":
        result["sources"][-1].update(origin="ocr", numeric_verified=False)
    elif change == "child_identity":
        child = child.model_copy(update={"volume": "999"})
    else:
        proposal = next(p for p in result["parameter_proposals"] if p["parameter"] == "quantity")
        if change == "unit":
            proposal["unit"] = "т"
        elif change == "scope":
            proposal["scope_state"] = "unresolved"
        else:
            proposal.update(value="2", unit="т", quote="2 т")
            result["sources"][-1]["text"] = "Собственный выход материала: 2 т"
    output = bind(record, result, positions, context, child)
    checked = enrich(child, output, positions, dict(parent={"index": 0}))
    assert not verified_geometry_dependency(checked, child)


@pytest.mark.parametrize("usable", ["stale", "empty"])
def test_unusable_donor_does_not_require_another_parent_model_call(usable):
    record, result, positions, context = current_fixture()
    donor, *_ = cast(Any, fixture())
    if usable == "stale":
        donor["payload"]["vor_version_id"] = "old"
    else:
        # Pass identity guards, but the donor supplies no project geometry.
        from human_basis import position_key
        donor["payload"]["position_key"] = position_key(positions[0])
        donor["payload"]["position_check"].update(sources=[], parameter_proposals=[], parameter_cards=[])
    context.repository.normative_reviews.return_value = [donor]
    output = bind_parent_basis(context, record, result, positions, dict(parent={"index": 0}),
        expected_kb={"release": "other"}, position=positions[1])
    checked = enrich(positions[1], output, positions, dict(parent={"index": 0}))
    assert verified_geometry_dependency(checked, positions[1])
    assert any(e.get("basis") == "current_material_literal_geometry" for e in output["evidence_trace"])
