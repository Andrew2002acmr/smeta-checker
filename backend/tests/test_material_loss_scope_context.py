"""Addressed loss excerpts never classify quantities or decide claim states."""

from copy import deepcopy
import json
from typing import Any, cast

import pytest

import normative_methodology_context as method
from test_normative_methodology_context import FIXTURE, PIN, corpus as corpus

# Independent literal fixture: full paragraphs, including the continued p23.
READINGS = json.loads(
    r"""[{"label": "6", "text": "6. Нормативы трудноустранимых потерь и отходов материальных ресурсов, приведенные в Методике, предназначены: а) для учета в сметной документации нормативного расхода материальных ресурсов с учетом величины трудноустранимых потерь и отходов (%) в случае, когда количество материальных ресурсов зависит от проектных решений и обозначается в сборниках ГЭСН литерой «П», при этом нормы потерь и отходов отсутствуют в общих положениях сборников ГЭСН; б) для определения потребности в материальных ресурсах, учитывающих величину трудноустранимых потерь и отходов при разработке сметных норм на строительные, специальные строительные, ремонтно-строительные и реставрационные работы.", "physical_pages": [3], "text_sha256": "ae4f24e3bcfb8e75dbf99e3c8de268abc4aa8088328b165b83bd02483d19b36e", "verification": "visual_literal_transcription_whitespace_normalized", "primary_renderings": [{"physical_page": 3, "sha256": "102c9d46623c7be05251e556ccaa95ce137073283a3a8d603a5af5e124ea623e"}], "independent_readings": 2}, {"label": "8", "text": "8. Для материальных ресурсов, расход которых зависит от проектных решений и обозначается в сборнике ГЭСН литерой «П», величина трудноустранимых потерь рассчитывается на основании общих положений рассматриваемого сборника ГЭСН и учитывается в общем расходе материального ресурса.", "physical_pages": [3], "text_sha256": "66254e7ed6ba136adf03c55414114d758df95194abaf1d01d53d3022406154c7", "verification": "visual_literal_transcription_whitespace_normalized", "primary_renderings": [{"physical_page": 3, "sha256": "102c9d46623c7be05251e556ccaa95ce137073283a3a8d603a5af5e124ea623e"}], "independent_readings": 2}, {"label": "22", "text": "22. Определение размера трудноустранимых потерь и отходов осуществляется в процессе определения общего расхода материальных ресурсов с использованием формулы общего расхода материальных ресурсов (1):\nP_Σ(ij) = P_п(ij) + P_то(ij) + P_тп(ij) + P_проч (1),\nгде:\nP_Σ(ij) — суммарный расход материальных ресурсов i-го класса j-го вида;\nP_п(ij) — полезный расход материальных ресурсов i-го класса j-го вида, т.е. полноценное потребление материальных ресурсов на производство продукции или объем работ (без учета отходов и потерь);\nP_то(ij) — суммарные технологические отходы материальных ресурсов i-го класса j-го вида, обусловленные особенностями технологического процесса производства продукции или объемом работ;\nP_тп(ij) — суммарные технологические потери материальных ресурсов i-го класса j-го вида, обусловленные особенностями технологического процесса производства продукции или объемом работ;\nP_проч — прочие организационно-технические отходы и потери, обусловленные причинами, не зависящими от технологического процесса.", "physical_pages": [8], "text_sha256": "43523bac3cf2952ba1a4e5402f3dbb0ec2fc885f523d3590e1d69e4cd639845a", "verification": "visual_literal_transcription_formula_linearized", "primary_renderings": [{"physical_page": 8, "sha256": "8164f0fd13d40792045f236fba8ffdc6dd20916ddacfd255209dd5bf60b2d0d8"}], "formula_notation": {"kind": "linearized_visual_formula", "formula": "P_Σ(ij) = P_п(ij) + P_то(ij) + P_тп(ij) + P_проч (1)", "transformation": "Printed mathematical P and subscripts serialized using P_ and parenthesized indices; definitions retained. Not native text, no inferred percentages or scales."}, "independent_readings": 2}, {"label": "23", "text": "23. Полезный расход материальных ресурсов определяется на основе реализуемых в проектной и иной технической документации по объекту строительства: норм, стандартов, результатов соответствующих прочностных, технологических и иных расчетов, а также технологий соответствующих строительных процессов (далее — полезный расход).", "physical_pages": [8, 9], "text_sha256": "c5dd30e15e4aeb7d3f07c5ceb6a6a9682e28e7f4cc13e5c37e119a27bf8c8545", "verification": "visual_literal_transcription_whitespace_normalized", "primary_renderings": [{"physical_page": 8, "sha256": "8164f0fd13d40792045f236fba8ffdc6dd20916ddacfd255209dd5bf60b2d0d8"}, {"physical_page": 9, "sha256": "46b1165b5e7e670fafec53d20772c04a3bfad81140455f5908c8b54e85c1a748"}], "independent_readings": 2}]"""
)


@pytest.fixture
def material_corpus(corpus):
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    label, key, pages, _ = method.MATERIAL_CLAUSES[1]
    clause = next(c for c in base["reviewed_clauses"] if c["label"] == label)
    corpus.entities[key] = dict(
        id=key,
        entity_kind="clause",
        source_id=method.SOURCE_ID,
        source_kind="METHODOLOGY",
        designation=method.DESIGNATION,
        label=label,
        text=clause["text"],
        status=method.REVIEW_STATUS,
        edition=base,
        citations=[
            dict(
                source_id=method.SOURCE_ID,
                physical_page=p,
                source_sha256=method.SOURCE_SHA256,
                text_provenance=method.TRANSCRIPTION,
            )
            for p in pages
        ],
    )
    corpus.db.execute("ALTER TABLE sources ADD COLUMN metadata_json TEXT")
    original = dict(
        sha256=method.LOSS_SOURCE_SHA256,
        role="material_loss_methodology",
        status="pdf_verified",
        ingestion="reviewed_loss_methodology",
        reviewed_clauses=deepcopy(READINGS),
    )
    amendment = dict(
        sha256=method.LOSS_AMENDMENT_SHA256,
        role="material_loss_methodology_amendment",
        status="pdf_verified",
        ingestion="reviewed_loss_methodology",
        amends_source_sha256=method.LOSS_SOURCE_SHA256,
        unchanged_reviewed_clauses=["6", "8", "22", "23"],
        verified_change_scope="clause12_and_appendix4_only",
        verification="two_independent_visual_readings",
    )
    for sid, sha, designation, count, metadata in (
        (
            method.LOSS_SOURCE_ID,
            method.LOSS_SOURCE_SHA256,
            method.LOSS_DESIGNATION,
            44,
            original,
        ),
        (
            method.LOSS_AMENDMENT_ID,
            method.LOSS_AMENDMENT_SHA256,
            method.LOSS_AMENDMENT_DESIGNATION,
            2,
            amendment,
        ),
    ):
        corpus.source_index[sid] = dict(designation=designation, pages=count)
        corpus.db.execute(
            "INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
            (
                sid,
                sha,
                "blobs/" + sha + ".pdf",
                "METHODOLOGY",
                designation,
                count,
                json.dumps(metadata),
            ),
        )
    for label, key, pages, _ in method.LOSS_CLAUSES:
        reading = next(r for r in READINGS if r["label"] == label)
        corpus.entities[key] = dict(
            id=key,
            entity_kind="clause",
            source_id=method.LOSS_SOURCE_ID,
            source_kind="METHODOLOGY",
            designation=method.LOSS_DESIGNATION,
            label=label,
            text=reading["text"],
            status=method.REVIEW_STATUS,
            edition=deepcopy(original),
            citations=[
                dict(
                    source_id=method.LOSS_SOURCE_ID,
                    physical_page=p,
                    source_sha256=method.LOSS_SOURCE_SHA256,
                    text_provenance=reading["verification"],
                )
                for p in pages
            ],
        )
    return corpus


def get(kb):
    return method.methodology_context(kb, [], expected_pin=PIN, material=True)


def test_complete_optional_bundle_retains_scope_and_full_571(material_corpus):
    kb = material_corpus
    before = deepcopy(kb.entities)
    result = get(kb)
    assert result["loss_context_reason"] == "reviewed_addressed_loss_context"
    literals = result["context"]["literal_clauses"]
    assert [c["clause"] for c in literals] == ["7", "12", "6", "8", "22", "23"]
    assert len(result["sources"]) == 6
    assert "Не допускается применение" in literals[0]["quote"]
    assert "с учетом трудноустранимых потерь" in literals[1]["quote"]
    assert "в сметной документации" in literals[2]["quote"]
    assert "без учета отходов и потерь" in literals[4]["quote"]
    assert "технологических и иных расчетов, а также технологий" in literals[5]["quote"]
    assert (
        result["context"]["loss_basis"]["amendment_sha256"]
        == method.LOSS_AMENDMENT_SHA256
    )
    assert kb.entities == before
    assert all("state" not in s and "confirmed" not in s for s in result["sources"])
    assert "loss_required" not in result["context"]


def test_formula_is_marked_as_linearized_with_primary_render_provenance(
    material_corpus,
):
    result = get(material_corpus)
    literal = next(
        c
        for c in result["context"]["literal_clauses"]
        if c.get("document_source_id") and c["clause"] == "22"
    )
    assert literal["formula_notation"]["formula"] == method.LOSS_FORMULA
    assert literal["text_provenance"] == method.LOSS_FORMULA_TRANSCRIPTION
    assert literal["primary_renderings"][0]["physical_page"] == 8
    assert len(literal["primary_renderings"][0]["sha256"]) == 64
    assert "100" not in literal["formula_notation"]["formula"]


@pytest.mark.parametrize("label", ["6", "8", "22", "23"])
def test_missing_or_altered_loss_clause_skips_whole_optional_set_only(
    material_corpus, label
):
    key = next(key for name, key, _, _ in method.LOSS_CLAUSES if name == label)
    material_corpus.entities[key]["text"] += " altered"
    result = get(material_corpus)
    assert [c["clause"] for c in result["context"]["literal_clauses"]] == ["7", "12"]
    assert result["loss_context_reason"] == "reviewed_loss_clause_set_unavailable"


@pytest.mark.parametrize(
    "mutation",
    [
        "source_sha",
        "amendment_sha",
        "blob_path",
        "role",
        "amendment_scope",
        "amendment_target",
        "source_review",
        "render",
        "render_sha",
        "formula",
    ],
)
def test_source_and_amendment_binding_fail_closed(material_corpus, mutation):
    kb = material_corpus
    if mutation in ("source_sha", "amendment_sha", "blob_path"):
        sid = (
            method.LOSS_AMENDMENT_ID
            if mutation == "amendment_sha"
            else method.LOSS_SOURCE_ID
        )
        field = "blob_path" if mutation == "blob_path" else "sha256"
        kb.db.execute("UPDATE sources SET " + field + "=? WHERE id=?", ("wrong", sid))
    else:
        sid = (
            method.LOSS_AMENDMENT_ID
            if mutation.startswith("amendment")
            else method.LOSS_SOURCE_ID
        )
        metadata = json.loads(
            kb.db.execute(
                "SELECT metadata_json FROM sources WHERE id=?", (sid,)
            ).fetchone()[0]
        )
        if mutation == "role":
            metadata["role"] = "unreviewed"
        elif mutation == "amendment_scope":
            metadata["verified_change_scope"] = "whole_consolidation"
        elif mutation == "amendment_target":
            metadata["amends_source_sha256"] = "0" * 64
        else:
            for reading in metadata["reviewed_clauses"]:
                if mutation == "source_review":
                    reading["independent_readings"] = 1
                elif mutation == "render":
                    reading["primary_renderings"] = []
                elif mutation == "render_sha":
                    reading["primary_renderings"][0]["sha256"] = "a" * 64
                elif reading["label"] == "22":
                    reading["formula_notation"]["formula"] += " * 100"
            for _, key, _, _ in method.LOSS_CLAUSES:
                kb.entities[key]["edition"] = deepcopy(metadata)
        kb.db.execute(
            "UPDATE sources SET metadata_json=? WHERE id=?", (json.dumps(metadata), sid)
        )
    result = get(kb)
    assert len(result["sources"]) == 2
    assert [c["clause"] for c in result["context"]["literal_clauses"]] == ["7", "12"]
    assert "loss_basis" not in result["context"]


def test_loss_budget_failure_preserves_both_full_base_clauses(
    material_corpus, monkeypatch
):
    monkeypatch.setattr(method, "MAX_LOSS_BUNDLE_UTF8_BYTES", 100)
    result = get(material_corpus)
    assert len(result["sources"]) == 2
    assert result["loss_context_reason"] == "reviewed_loss_context_budget_exceeded"


def test_optional_loss_must_not_bypass_missing_571_scope(material_corpus):
    del material_corpus.entities[method.MATERIAL_CLAUSES[0][1]]
    assert get(material_corpus)["sources"] == []


def test_work_method_context_does_not_gain_material_loss_bundle(material_corpus):
    result = method.methodology_context(
        material_corpus, [dict(claim_id="method:equipment")], expected_pin=PIN
    )
    assert [c["clause"] for c in result["context"]["literal_clauses"]] == [
        "5",
        "7",
        "22",
    ]
    assert "loss_basis" not in result["context"]


def test_identity_change_during_optional_reads_never_returns_mixed_context(
    material_corpus,
):
    original = material_corpus.entity

    def switched(kind, key):
        result = original(kind, key)
        if key == method.LOSS_CLAUSES[0][1]:
            material_corpus.identity = dict(PIN, sha256="f" * 64)
        return result

    material_corpus.entity = switched
    assert get(material_corpus)["sources"] == []


def test_full_loss_literals_survive_compaction_and_source_aliasing(
    material_corpus, monkeypatch
):
    from normative_agent import compact, model_source_aliases, PIPELINE_VERSION
    from normative_context_packing import serialize_context

    result = get(material_corpus)
    sources = result["sources"]
    inputs = dict(
        methodology_context=result["context"],
        work_scope_graph=dict(positions=[], same_work_other_scopes=[]),
        untrusted_other_data="x" * 24000,
    )
    metadata = dict(
        inputs,
        sources=[],
        archived_source_ids=[s["id"] for s in sources],
        matching_checks=[],
        pipeline_version=PIPELINE_VERSION,
    )
    monkeypatch.setattr(
        "normative_agent.MAX_CONTEXT", len(serialize_context(metadata)) + 500
    )
    packed = compact(inputs, sources, result["preferred_source_ids"])
    sent, aliases = model_source_aliases(packed, sources)
    sent = cast(dict[str, Any], sent)
    assert len(sent["methodology_context"]["literal_clauses"]) == 6
    for original, literal in zip(
        result["context"]["literal_clauses"],
        sent["methodology_context"]["literal_clauses"],
        strict=True,
    ):
        assert original["quote"] == literal["quote"]
        assert aliases[literal["source_id"]] == original["source_id"]
