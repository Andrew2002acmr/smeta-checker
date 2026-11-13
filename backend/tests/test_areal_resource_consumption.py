"""A dimensioned literal resource proves numeric conversion, never semantic scope."""

from copy import deepcopy
from typing import Any

import pytest

from normative_parameters import enrich
from normative_resource_consumption import EVENT
from normative_workbook_rate import geometric_rate_replay_supported
from test_normative_resource_consumption import fixture as resource_fixture


def definition(title="Обработка площадей", base="га"):
    return (
        "Таблица ГЭСН 27-06-035 "
        + title
        + "\nИзмеритель: "
        + base
        + "\n27-06-035-01 "
        + title
        + "\nСостав работ:\n01. "
        + title
        + "."
    )


def fixture(base="га", amount="200"):
    raw: Any = resource_fixture(amount, "кг", base, "Семена газонных трав")
    work, owner, result = raw
    work.work_name = "Семена газонных трав (смесь)"
    work.unit = "кг"
    work.volume = "38.62"
    work.comment = "Расход 2кг/100м2"
    work.formula = "1931*2/100"
    work.calculation = {"expression": work.formula}
    result["sources"][0]["label"] = "ГЭСН 27 · 27-06-035-01"
    result["sources"][0]["text"] = definition(base=base)
    result["sources"][1].update(
        text="Устройство газонов. Площадь 1931 м2", version_id="project-pin"
    )
    result["parameter_proposals"] = [
        dict(
            parameter="quantity",
            value="1931",
            unit="м2",
            source_id="p",
            quote="Площадь 1931 м2",
            scope_quote="Устройство газонов. Площадь 1931 м2",
            scope_state="verified",
            scope_relation="same_layer",
            operation="direct",
            operand_source_ids=[],
        )
    ]
    result["items"] += [
        dict(
            topic="Area",
            claim_ids=["input:area"],
            state="confirmed",
            project_source_ids=["p"],
            normative_source_ids=[],
            explanation="Area bound to current subject",
        ),
        dict(
            topic="Quantity",
            claim_ids=["quantity"],
            state="confirmed",
            project_source_ids=["p"],
            normative_source_ids=[],
            explanation="Calculated output",
        ),
    ]
    return work, result


def run(work, result):
    return enrich(work, result, {work.index: work}, {})


@pytest.mark.parametrize(
    "base,amount", [("га", "200"), ("100 м2", "2"), ("10000 м2", "200")]
)
def test_area_mass_recipe_from_selected_typed_resource(base, amount):
    work, result = fixture(base, amount)
    original = deepcopy(result)
    output = run(work, result)
    cards = {c["id"]: c for c in output["parameter_cards"]}
    assert (
        cards["quantity"]["state"] == "confirmed"
        and cards["quantity"]["found_value"] == "38.62"
    )
    assert cards["quantity"]["role"] == "calculation"
    assert cards["factor:1:2"]["state"] == "confirmed"
    assert cards["factor:1:2"]["found_unit"] == "кг/100м2"
    assert "не буквальная" in cards["quantity"]["reason"]
    assert result == original and output["state"] == "needs_facts"
    assert geometric_rate_replay_supported(work, output)


@pytest.mark.parametrize(
    "mutation",
    [
        "app_unknown",
        "no_event",
        "bad_area",
        "scope_unknown",
        "ocr_area",
        "ocr_resource",
        "ocr_norm",
        "norm_unit",
        "resource_unit",
        "resource_name",
        "resource_quantity",
        "project_rate",
        "other_pdf",
        "conditions",
        "extra_factor",
        "divisor",
        "arithmetic",
        "literal_output",
        "flower",
    ],
)
def test_raw_mutations_and_non_mass_recipes_do_not_acquire_proof(mutation):
    work, result = fixture()
    event = next(e for e in result["evidence_trace"] if e["kind"] == EVENT)
    if mutation == "app_unknown":
        result["items"][0]["state"] = "needs_facts"
    if mutation == "no_event":
        result["evidence_trace"] = []
    if mutation == "bad_area":
        result["sources"][1]["text"] = "Устройство газонов. Площадь 999 м2"
    if mutation == "scope_unknown":
        result["parameter_proposals"][0]["scope_state"] = "unresolved"
    if mutation == "ocr_area":
        result["sources"][1]["origin"] = "ocr_unreviewed"
    if mutation == "ocr_resource":
        result["sources"][-1]["origin"] = "ocr_unreviewed"
    if mutation == "ocr_norm":
        result["sources"][0]["origin"] = "ocr_unreviewed"
    if mutation == "norm_unit":
        result["sources"][0]["text"] = "Измеритель: м2"
    if mutation == "resource_unit":
        event["resources"][0]["unit"] = "т"
    if mutation == "resource_name":
        event["resources"][0]["name"] = "Семена цветочных культур"
    if mutation == "resource_quantity":
        event["resources"][0]["quantity"] = "201"
    if mutation == "project_rate":
        event["resources"][0].update(quantity_kind="project", raw_quantity="П")
    if mutation == "other_pdf":
        result["sources"][0]["source_id"] = "other-pdf"
    if mutation == "conditions":
        event["checks"] = [{"state": "unknown", "label": "Terrain"}]
    if mutation == "extra_factor":
        work.calculation["expression"] = "1931*2/100*2"
    if mutation == "divisor":
        work.calculation["expression"] = "1931*2/1000"
    if mutation == "arithmetic":
        work.volume = "99"
    if mutation == "literal_output":
        work.comment = "Расход 2л/100м2"
    if mutation == "flower":
        work.work_name = "Семена цветочных культур"
    output = run(work, result)
    cards = {c["id"]: c for c in output["parameter_cards"]}
    assert cards["quantity"]["state"] != "confirmed"
    assert not any("resource_rate_basis" in e for e in output["evidence_trace"])


def test_ambiguous_matching_rows_not_selected_by_numeric_agreement():
    work, result = fixture()
    event = result["evidence_trace"][0]
    other = {
        **event["resources"][0],
        "resource_code": "99",
        "quantity": "300",
        "raw_quantity": "300",
        "quote": "99 Семена газонных трав; кг: 300",
    }
    event["resources"].append(other)
    result["sources"][-1]["text"] += "\n" + other["quote"]
    assert (
        next(c for c in run(work, result)["parameter_cards"] if c["id"] == "quantity")[
            "state"
        ]
        == "needs_facts"
    )


def test_stored_green_cards_cannot_replace_current_area_or_resource_on_replay():
    work, result = fixture()
    output = run(work, result)
    assert geometric_rate_replay_supported(work, output)
    for mutation in ["project", "resource", "proposal", "norm_measure"]:
        changed = deepcopy(output)
        if mutation == "project":
            changed["sources"][1]["text"] = "Other site area 1931 м2"
        if mutation == "resource":
            changed["sources"][-1]["text"] = "Mutated row"
        if mutation == "proposal":
            changed["parameter_proposals"][0]["scope_state"] = "unresolved"
        if mutation == "norm_measure":
            changed["sources"][0]["text"] = "Измеритель: 100 м2"
        assert not geometric_rate_replay_supported(work, changed), mutation


def test_semantic_unknown_is_preserved_even_when_another_applicability_item_is_confirmed():
    work, result = fixture()
    result["items"].append(
        dict(
            topic="Property not established",
            claim_ids=["material:property"],
            state="needs_facts",
            project_source_ids=["p"],
            normative_source_ids=["n"],
            explanation="Unknown property",
        )
    )
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "confirmed"
    )
    assert (
        next(i for i in output["items"] if i["topic"] == "Property not established")[
            "state"
        ]
        == "needs_facts"
    )
    assert output["state"] == "needs_facts"


def test_only_exact_structured_material_to_area_unit_check_can_be_resolved():
    work, result = fixture()
    event = result["evidence_trace"][0]
    check = dict(
        label="Единица ВОР",
        state="mismatch",
        actual="кг",
        expected="га",
        norm_quantity=None,
    )
    event["checks"] = [check]
    assert (
        next(c for c in run(work, result)["parameter_cards"] if c["id"] == "quantity")[
            "state"
        ]
        == "confirmed"
    )
    for key, value in [
        ("label", "Some unit check"),
        ("state", "unknown"),
        ("actual", "т"),
        ("expected", "м2"),
    ]:
        changed = deepcopy(result)
        changed["evidence_trace"][0]["checks"][0][key] = value
        assert (
            next(
                c
                for c in run(work, changed)["parameter_cards"]
                if c["id"] == "quantity"
            )["state"]
            != "confirmed"
        )
    event["checks"].append(dict(label="Layer applicability", state="mismatch"))
    assert (
        next(c for c in run(work, result)["parameter_cards"] if c["id"] == "quantity")[
            "state"
        ]
        != "confirmed"
    )


def test_direct_output_disagreement_is_not_erased_by_computing_expected_quantity():
    work, result = fixture()
    result["sources"].append(
        dict(
            id="mass",
            kind="project",
            origin="native",
            label="PD",
            version_id="project-pin",
            page=2,
            text="Семена газонных трав: масса 50 кг",
        )
    )
    result["parameter_proposals"].append(
        dict(
            parameter="quantity",
            value="50",
            unit="кг",
            source_id="mass",
            quote="масса 50 кг",
            scope_quote="Семена газонных трав: масса 50 кг",
            scope_state="verified",
        )
    )
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "possible_mismatch"
    )


@pytest.mark.parametrize(
    "title",
    [
        "Посев луговых газонов тракторной сеялкой",
        "Посев газонов партерных, мавританских и обыкновенных вручную",
    ],
)
def test_literal_qualified_lawn_definition_cannot_use_generic_area_despite_model_applicability(
    title,
):
    work, result = fixture()
    result["sources"][0]["text"] = definition(title)
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    event = next(
        e for e in output["evidence_trace"] if e["kind"] == "workbook_area_rate_v1"
    )
    assert (
        event["resource_scope_gaps"] and "категори" in event["resource_scope_gaps"][0]
    )
    assert not event.get("resource_rate_basis")


@pytest.mark.parametrize(
    "title,scope,confirmed",
    [
        (
            "Посев луговых газонов тракторной сеялкой",
            "Устройство луговых газонов тракторной сеялкой.",
            True,
        ),
        (
            "Посев луговых газонов тракторной сеялкой",
            "Устройство обыкновенных газонов.",
            False,
        ),
        (
            "Посев газонов партерных, мавританских и обыкновенных вручную",
            "Устройство обыкновенных газонов.",
            False,
        ),
        (
            "Посев газонов партерных, мавританских и обыкновенных вручную",
            "Устройство обыкновенных газонов вручную.",
            True,
        ),
        (
            "Посев газонов вручную",
            "Устройство газонов механизированным способом.",
            False,
        ),
    ],
)
def test_category_and_manual_scope_require_positive_project_area_anchor(
    title, scope, confirmed
):
    work, result = fixture()
    result["sources"][0]["text"] = definition(title)
    result["sources"][1]["text"] = scope + " Площадь 1931 м2"
    result["parameter_proposals"][0]["scope_quote"] = result["sources"][1]["text"]
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "confirmed"
    ) is confirmed


def test_unrelated_page_cannot_supply_category_missing_in_verified_area_scope():
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    result["sources"].append(
        dict(
            id="other",
            kind="project",
            origin="native",
            version_id="project-pin",
            page=4,
            text="Луговые газоны на другом участке",
        )
    )
    result["items"][0]["project_source_ids"].append("other")
    assert (
        next(c for c in run(work, result)["parameter_cards"] if c["id"] == "quantity")[
            "state"
        ]
        == "needs_facts"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("quantity", "999"),
        ("input:area", "999"),
        ("factor:1:2", "999"),
    ],
)
def test_replayed_areal_proof_does_not_authorize_changed_green_cards(key, value):
    work, result = fixture()
    output = run(work, result)
    assert geometric_rate_replay_supported(work, output)
    next(c for c in output["parameter_cards"] if c["id"] == key)["found_value"] = value
    assert not geometric_rate_replay_supported(work, output)


def test_missing_literal_category_creates_required_applicability_finding_idempotently():
    work, result = fixture()
    result["state"] = "confirmed"
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    output = run(work, result)
    for review in (output, run(work, output)):
        findings = [
            i
            for i in review["items"]
            if i["topic"] == "Область применения размерного расхода ресурса"
        ]
        assert len(findings) == 1
        assert findings[0]["state"] == "needs_facts" and findings[0]["required"]
        assert findings[0]["claim_ids"] == ["normative:applicability"]
        assert findings[0]["project_source_ids"] == ["p"]
        assert set(findings[0]["normative_source_ids"]) == {
            "n",
            result["sources"][-1]["id"],
        }
        assert (
            next(c for c in review["parameter_cards"] if c["id"] == "input:area")[
                "state"
            ]
            == "confirmed"
        )
        assert (
            next(c for c in review["parameter_cards"] if c["id"] == "calculation:1")[
                "state"
            ]
            == "confirmed"
        )
        assert review["items"][0]["state"] == "confirmed"
        assert review["state"] == "needs_facts"


def test_positive_project_reassessment_removes_only_recorded_server_finding():
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    model_unknown = dict(
        topic="Область применения размерного расхода ресурса",
        required=True,
        state="needs_facts",
        claim_ids=["normative:applicability"],
        project_source_ids=["p"],
        normative_source_ids=["n"],
        explanation="Model still lacks a separate operating condition",
    )
    result["items"].append(model_unknown)
    output = run(work, result)
    assert (
        len([i for i in output["items"] if i["topic"] == model_unknown["topic"]]) == 2
    )
    output["sources"][1]["text"] = (
        "Устройство луговых газонов тракторной сеялкой. Площадь 1931 м2"
    )
    output["parameter_proposals"][0]["scope_quote"] = output["sources"][1]["text"]
    revised = run(work, output)
    assert [i for i in revised["items"] if i["topic"] == model_unknown["topic"]] == [
        model_unknown
    ]
    assert (
        next(c for c in revised["parameter_cards"] if c["id"] == "quantity")["state"]
        == "confirmed"
    )
    assert revised["state"] == "needs_facts"


@pytest.mark.parametrize(
    "mutant",
    [
        "measure_only",
        "code_only",
        "wrong_code",
        "neighbor_code",
        "missing_composition",
        "no_family_label",
        "resource_label",
    ],
)
def test_incomplete_or_nonselected_norm_definition_cannot_bypass_qualified_scope(
    mutant,
):
    work, result = fixture()
    source = result["sources"][0]
    source["text"] = definition("Посев луговых газонов тракторной сеялкой")
    if mutant == "measure_only":
        source["text"] = "Измеритель: га"
    if mutant == "code_only":
        source["text"] = "27-06-035-01\nИзмеритель: га\nСостав работ:\n01. Посев."
    if mutant == "wrong_code":
        source["text"] = definition().replace("27-06-035-01", "27-06-035-02")
    if mutant == "neighbor_code":
        source["text"] = definition() + "\n27-06-035-02 Другая норма"
        # Put a sibling into the definition rather than borrowing its scope.
        source["text"] = source["text"].replace(
            "Состав работ:", "27-06-035-02 Другая норма\nСостав работ:"
        )
    if mutant == "missing_composition":
        source["text"] = definition().split("Состав работ:")[0]
    if mutant == "no_family_label":
        source["label"] = "Other · 27-06-035-01"
        source["text"] = definition()
    if mutant == "resource_label":
        source["label"] = "ГЭСН Ресурсы нормы 27-06-035-01"
        source["text"] = definition()
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        != "confirmed"
    )
    assert not any(e.get("resource_rate_basis") for e in output["evidence_trace"])


@pytest.mark.parametrize(
    "scope",
    [
        "Устройство луговых газонов вручную.",
        "Устройство луговых газонов.",
        "Устройство луговых газонов механизированным способом.",
    ],
)
def test_tractor_meadow_definition_requires_positive_method_in_same_pd_area_scope(
    scope,
):
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    result["sources"][1]["text"] = scope + " Площадь 1931 м2"
    result["parameter_proposals"][0]["scope_quote"] = result["sources"][1]["text"]
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    assert any(
        i["state"] == "needs_facts" and i["claim_ids"] == ["normative:applicability"]
        for i in output["items"]
    )


def test_unresolved_same_source_scope_cannot_supply_missing_category_or_method():
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    text = "На другом участке луговой газон тракторной сеялкой."
    result["sources"][1]["text"] += " " + text
    extra = {
        **result["parameter_proposals"][0],
        "scope_state": "unresolved",
        "scope_quote": result["sources"][1]["text"],
    }
    result["parameter_proposals"].append(extra)
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "input:area")["state"]
        == "confirmed"
    )
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    assert not any(e.get("resource_rate_basis") for e in output["evidence_trace"])


def test_table_heading_cannot_widen_selected_lawn_category_as_a_union():
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    ).replace(
        "Таблица ГЭСН 27-06-035 Посев луговых газонов тракторной сеялкой",
        "Таблица ГЭСН 27-06-035 Устройство обыкновенных газонов",
    )
    result["sources"][1]["text"] = (
        "Устройство обыкновенных газонов тракторной сеялкой. Площадь 1931 м2"
    )
    result["parameter_proposals"][0]["scope_quote"] = result["sources"][1]["text"]
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    assert not any(e.get("resource_rate_basis") for e in output["evidence_trace"])


@pytest.mark.parametrize("select_rogue", [False, True])
def test_same_code_same_pdf_rogue_definition_cannot_replace_selected_literal_definition(
    select_rogue,
):
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    rogue = {**result["sources"][0], "id": "rogue", "text": definition("Посев газонов")}
    result["sources"].append(rogue)
    result["evidence_trace"][0]["norm_source_id"] = "rogue"
    if select_rogue:
        result["items"][0]["normative_source_ids"].append("rogue")
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    assert not any(e.get("resource_rate_basis") for e in output["evidence_trace"])


@pytest.mark.parametrize(
    "scope",
    [
        "Устройство газонов посевом луговых трав тракторной сеялкой.",
        "Устройство газонов на луговых почвах тракторной сеялкой.",
        "Устройство газонов. На другом участке луговой газон тракторной сеялкой.",
    ],
)
def test_meadow_adjective_must_bind_lawn_kind_and_not_other_site(scope):
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    result["sources"][1]["text"] = scope + " Площадь 1931 м2"
    result["parameter_proposals"][0]["scope_quote"] = result["sources"][1]["text"]
    output = run(work, result)
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "needs_facts"
    )
    assert not any(e.get("resource_rate_basis") for e in output["evidence_trace"])


@pytest.mark.parametrize(
    "scope",
    [
        "Устройство луговых газонов тракторной сеялкой.",
        "Газоны луговые. Посев тракторной сеялкой.",
    ],
)
def test_lawn_category_literal_inflections_and_reverse_order_are_supported(scope):
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    result["sources"][1]["text"] = scope + " Площадь 1931 м2"
    result["parameter_proposals"][0]["scope_quote"] = result["sources"][1]["text"]
    assert (
        next(c for c in run(work, result)["parameter_cards"] if c["id"] == "quantity")[
            "state"
        ]
        == "confirmed"
    )


def test_resource_scope_gap_adds_required_unknown_to_existing_consumption_claims_only():
    work, result = fixture()
    result["sources"][0]["text"] = definition(
        "Посев луговых газонов тракторной сеялкой"
    )
    rate_claims = [
        "basis:comment:consumption:1",
        "quantity:consumption",
        "derived:rate",
    ]
    for claim in [*rate_claims, "material:property:literal_rate"]:
        result["items"].append(
            dict(
                topic=claim,
                state="confirmed",
                required=True,
                claim_ids=[claim],
                project_source_ids=["p"],
                normative_source_ids=["n"],
                explanation="Existing model conclusion",
            )
        )
    output = run(work, result)
    finding = next(
        i
        for i in output["items"]
        if i["topic"] == "Область применения размерного расхода ресурса"
    )
    assert finding["claim_ids"] == ["normative:applicability", *sorted(rate_claims)]
    for claim in rate_claims:
        relevant = [
            i
            for i in output["items"]
            if claim in i.get("claim_ids", []) and i.get("required", True)
        ]
        assert any(i["state"] == "needs_facts" for i in relevant)
    assert (
        next(
            i for i in output["items"] if i["topic"] == "material:property:literal_rate"
        )["state"]
        == "confirmed"
    )
    cards = {c["id"]: c for c in output["parameter_cards"]}
    assert (
        cards["input:area"]["state"] == "confirmed"
        and cards["calculation:1"]["state"] == "confirmed"
    )
    repeated = run(work, output)
    assert len([i for i in repeated["items"] if i["topic"] == finding["topic"]]) == 1
