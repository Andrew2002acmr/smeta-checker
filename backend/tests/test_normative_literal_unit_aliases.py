"""SI spelling aliases do not alter the literal magnitude or dimension."""

import pytest

from normative_parameters import measure, unit_quoted
from normative_parameter_feedback import literal_parameter_issues
from workspace_models import NormativeParameterProposal


@pytest.mark.parametrize(
    "unit,literal",
    [
        ("м", "0,07m"),
        ("m", "0,07м"),
        ("см", "7cm"),
        ("м2", "106 m²"),
        ("m3", "0,07м³"),
        ("т", "2t"),
        ("кг", "3kg"),
    ],
)
def test_measurement_and_citation_use_the_same_spelling_alias(unit, literal):
    assert unit_quoted(unit, literal)
    assert measure("1", unit)


@pytest.mark.parametrize(
    "unit,literal",
    [
        ("м", "7cm"),
        ("м", "7m²"),
        ("м2", "7m³"),
        ("т", "7kg"),
        ("м", "7meters"),
        ("м", "7marker"),
        ("м", "0,07"),
        ("", "0,07"),
    ],
)
def test_alias_does_not_supply_missing_unit_or_convert_scale_dimension_or_words(
    unit, literal
):
    assert not unit_quoted(unit, literal)


def test_actual_latin_m_thickness_quote_is_not_rewritten():
    source = dict(kind="project", text="А22Нт; h=0,07m")
    proposal = NormativeParameterProposal(
        parameter="thickness",
        value="0.07",
        unit="м",
        quote="h=0,07m",
        source_id="pd",
        scope_quote=source["text"],
        scope_state="verified",
    )
    assert literal_parameter_issues(proposal, source) == []
    assert proposal.quote == "h=0,07m" and source["text"] == "А22Нт; h=0,07m"


@pytest.mark.parametrize(
    "value,quote", [("20", "20cm"), ("5", "5см"), ("1", "1 м"), ("3", "3кг")]
)
def test_short_complete_dimensioned_literal_is_valid(value, quote):
    from normative_parameters import quoted

    assert quoted(value, quote, "Свойство: " + quote)


@pytest.mark.parametrize(
    "value,quote,text",
    [
        ("20", "20", "Толщина 20cm"),
        ("5", "5С", "Марка 5С"),
        ("20", "20cm", "Толщина 120cm"),
        ("5", "5см", "Слой 15см"),
        ("20", "20x", "20x"),
    ],
)
def test_short_bare_number_designation_or_numeric_substring_remains_invalid(
    value, quote, text
):
    from normative_parameters import quoted

    assert not quoted(value, quote, text)


@pytest.mark.parametrize(
    "verified",
    [
        None,
        {"parameter": "quantity", "value": "20", "unit": "см"},
        {"parameter": "thickness", "value": "21", "unit": "см"},
        {"parameter": "thickness", "value": "20", "unit": "см"},
    ],
)
def test_verified_cell_amount_does_not_verify_ocr_header_thickness(verified):
    source = dict(
        id="agent:p90:cell:readable",
        kind="project",
        origin="ocr",
        numeric_verified=True,
        text="Основание ЩПС (20cm)\nПлощадь: 100 м2",
        verified_parameters=[verified] if verified else [],
    )
    proposal = NormativeParameterProposal(
        parameter="thickness",
        value="20",
        unit="см",
        quote="20cm",
        source_id=source["id"],
        scope_quote="Основание ЩПС (20cm)",
        scope_state="verified",
    )
    issues = literal_parameter_issues(proposal, source)
    assert bool(issues) == (
        not verified
        or verified.get("parameter") != "thickness"
        or verified.get("value") != "20"
    )
