"""An acquired image cannot prove a claim before a validated visual reading."""
from copy import deepcopy

import pytest

from normative_fact_ledger import build as build_ledger
from normative_parameter_feedback import literal_parameter_issues
from normative_parameters import build_cards
from normative_position_check import validate_items
from normative_source_roles import unread_visual_source
from normative_verdict import readable_review, publish, REVIEW
from workspace_models import NormativeParameterProposal, NormativePositionCheckItem
from test_normative_parameters import position


def source(origin="drawing_region_unread", source_id="drawing-region:crop"):
    # Even a future text formatter/strong-looking flags cannot turn acquisition
    # into a verified visual interpretation.
    return dict(id=source_id, kind="project", version_id="pdf", page=1, label="Crop",
        origin=origin, text="Конструкция К-1: количество 400 шт", numeric_verified=True,
        unit_verified=True, unit="шт", rects=[[.1,.2,.4,.5]])


def item(state="confirmed", source_id="drawing-region:crop", claim="presence"):
    return NormativePositionCheckItem(topic="Claim", state=state, explanation="Claimed from a crop",
        claim_ids=[claim], project_source_ids=[source_id], normative_source_ids=[])


@pytest.mark.parametrize("state", ["confirmed", "possible_mismatch"])
@pytest.mark.parametrize("tag", ["origin", "namespace", "alias_namespace"])
def test_main_validator_rejects_crop_semantics_even_when_origin_is_stripped(state, tag):
    s = source()
    if tag != "origin":
        s["origin"] = "native"
    if tag == "alias_namespace":
        s.update(id="shared:0:transport", physical_source_id="drawing-region:crop")
    validated = validate_items([item(state, s["id"])], [s])
    assert validated[0]["state"] == "needs_facts"


def test_numeric_literal_cards_and_ledger_cannot_certify_unread_image_text():
    s = source()
    p = position(volume="400", unit="шт")
    p.work_name = "Конструкция К-1"
    proposal = NormativeParameterProposal(parameter="quantity", value="400", unit="шт", source_id=s["id"],
        quote="количество 400 шт", scope_quote=s["text"], scope_state="verified")
    assert literal_parameter_issues(proposal, s)
    result = dict(pipeline_version=27, sources=[s], parameter_proposals=[proposal.model_dump()], items=[])
    result["parameter_cards"] = build_cards(p, result, {0:p}, {})
    assert result["parameter_cards"][0]["state"] == "needs_facts"
    fact = build_ledger(p, result, {0:p}, {})["facts"][0]
    assert fact["literal_state"] == fact["subject_binding_state"] == "unresolved"


def test_readable_saved_view_preserves_independent_native_facts_and_immutable_original():
    crop = source()
    native = source("native", "native")
    review = dict(state="confirmed", sources=[crop, native], items=[item().model_dump(),
        item(source_id="native", claim="quantity").model_dump()], parameter_cards=[
            dict(id="quantity", label="Quantity", role="project_value", state="confirmed", reason="Read",
                declared_value="400", found_value="400", unit="шт", found_unit="шт", source_ids=["native"]),
            dict(id="input:area", label="Crop", role="project_value", state="possible_mismatch", reason="Image",
                declared_value="400", unit="шт", source_ids=[crop["id"]])])
    frozen = deepcopy(review)
    rendered = readable_review(review)
    assert rendered["items"][0]["state"] == "needs_facts"
    assert rendered["items"][1]["state"] == "confirmed"
    assert rendered["parameter_cards"][0]["state"] == "confirmed"
    assert rendered["parameter_cards"][1]["state"] == "needs_facts"
    assert rendered["state"] == "needs_facts"
    assert rendered["sources"] == review["sources"]
    assert review == frozen
    assert readable_review(rendered) == rendered


@pytest.mark.parametrize("state", ["confirmed", "possible_mismatch"])
def test_raw_saved_crop_claim_never_publishes_confirmation_or_error(state):
    crop = source()
    native = source("native", "native")
    norm = dict(id="norm", kind="normative", origin="native", text="A norm condition")
    norm_item = dict(topic="Norm", state="confirmed", explanation="Independent condition",
        claim_ids=["normative:applicability"], project_source_ids=["native"], normative_source_ids=["norm"])
    review = dict(position_key="key", pipeline_version=27, execution_state="complete", state=state,
        sources=[crop, native, norm], items=[item(state).model_dump(),norm_item], parameter_cards=[])
    report = publish(dict(**{"Статус аудита":"ОК"}),
        dict(status="ОК", checks=[dict(claim_id="presence",state="not_found",required=True)]), "key",
        dict(stage="done",review=review),enabled=True)
    assert report["Статус аудита"] == REVIEW
    assert review["items"][0]["state"] == state


def test_existing_drawing_reading_namespace_is_not_confused_with_unread_crop():
    reviewed = source("visual_reading", "drawing:verified-reading")
    assert not unread_visual_source(reviewed)
    assert validate_items([item(source_id=reviewed["id"])], [reviewed])[0]["state"] == "confirmed"


def test_navigation_archive_handle_is_not_a_document_citation():
    validated = validate_items([item(source_id="tool-result:" + "a" * 32)], [])
    assert validated[0]["state"] == "needs_facts"
    assert not validated[0]["project_source_ids"]


def test_direct_finalizer_persists_the_same_safe_individual_assertions():
    from normative_verdict import finalize_state

    crop, native = source(), source("native", "native")
    review = dict(pipeline_version=27, execution_state="complete", state="confirmed",
        sources=[crop, native], items=[item().model_dump(),item(source_id="native", claim="quantity").model_dump()],
        parameter_cards=[])
    original = deepcopy(review)
    final = finalize_state(review, dict(status="ОК",checks=[]), "key")
    assert final["state"] == "needs_facts"
    assert final["items"][0]["state"] == "needs_facts"
    assert final["items"][1]["state"] == "confirmed"
    assert review == original
