"""Offline evidence-bound class context; no provider, project DB or approvals."""

from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import normative_manufacturer_context as maker

PIN = dict(
    database="releases/home-5a7370bcaeb18050d3d5f6a3/knowledge.sqlite",
    release_id="home-5a7370bcaeb18050d3d5f6a3",
    profile="roads",
    sha256="e7371dad6abb16c28b97c08aba247c66ffb76779857bac124c812c9907683108",
)
COMMENT = "Уплотнение основания производится тандемным виброкатком Hamm HD130"
CLAIMS = [
    dict(
        claim_id="method:comment:equipment:1",
        predicate="text",
        expected_text="Уплотнение основания производится тандемным виброкатком",
    )
]


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    entries = json.loads(
        (Path(__file__).parent / "fixtures/manufacturer-hd130-reviewed.json").read_text(
            encoding="utf-8"
        )
    )
    kb = SimpleNamespace(
        identity=deepcopy(PIN), root=tmp_path, source_index={}, entities={}, reads=[]
    )
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE sources(id TEXT,sha256 TEXT,blob_path TEXT,kind TEXT,designation TEXT,pages INTEGER)"
    )
    for expected, entry in zip(maker.SOURCES, entries, strict=True):
        sid, entity = expected["source_id"], expected["clause_id"]
        meta = deepcopy(entry)
        if entry["kind"] == "MANUFACTURER_WEB_CAPTURE":
            meta["provenance"]["raw_html_blob_path"] = (
                "provenance/" + maker.HTML_SHA + ".html"
            )
        kb.source_index[sid] = dict(
            designation=entry["designation"], pages=entry["pages"]
        )
        db.execute(
            "INSERT INTO sources VALUES(?,?,?,?,?,?)",
            (
                sid,
                entry["sha256"],
                "blobs/" + entry["sha256"] + ".pdf",
                entry["kind"],
                entry["designation"],
                entry["pages"],
            ),
        )
        kb.entities[entity] = dict(
            id=entity,
            entity_kind="clause",
            source_id=sid,
            source_kind=entry["kind"],
            designation=entry["designation"],
            label=expected["label"],
            text=entry["reviewed_clauses"][0]["text"],
            status=maker.REVIEW,
            edition=meta,
            citations=[
                dict(
                    source_id=sid,
                    physical_page=expected["page"],
                    source_sha256=entry["sha256"],
                    text_provenance=maker.REVIEW,
                    artifact_type=expected["artifact_type"],
                )
            ],
        )

    def entity(kind, key):
        assert kind == "clause"
        kb.reads.append(key)
        return deepcopy(kb.entities[key])

    kb.entity = entity
    kb.connect = lambda: nullcontext(db)
    kb.db = db
    # Source-byte verifier has its own real-file tests and the real staged-corpus
    # probe; these unit cases isolate DB/entity/provenance/context contracts.
    monkeypatch.setattr(maker, "_verified_file", lambda *args: True)
    yield kb
    db.close()


def run(kb, comment=COMMENT, claims=CLAIMS, pin=None):
    return maker.manufacturer_context(
        kb, comment, claims, expected_pin=pin or kb.identity
    )


def test_exact_comment_gets_both_literals_with_provenance_and_no_promotion(corpus):
    entities, claims = deepcopy(corpus.entities), deepcopy(CLAIMS)
    result = run(corpus)
    assert result["reason"] == "reviewed_model_class_context"
    assert len(result["sources"]) == len(result["context"]["literal_clauses"]) == 2
    assert result["preferred_source_ids"] == [s["id"] for s in result["sources"]]
    assert result["context"]["scope"] == "manufacturer_model_class_only"
    assert result["context"]["claim_ids"] == [CLAIMS[0]["claim_id"]]
    assert result["context"]["kb"] == PIN
    assert all(s["kb_identity"] == PIN for s in result["sources"])
    assert all(s['evidence_role'] == 'manufacturer_equipment_classification' for s in result['sources'])
    assert all(s['classification_claim_ids'] == [CLAIMS[0]['claim_id']] for s in result['sources'])
    assert all(
        hashlib.sha256(s["text"].encode()).hexdigest() == s["text_sha256"]
        for s in result["sources"]
    )
    assert not any("state" in s or "confirmed" in s for s in result["sources"])
    assert "GENERATED WEB CAPTURE, NOT factory PDF" in result["sources"][1]["label"]
    assert "не подтверждает пригодность" in result["context"]["instruction"]
    assert corpus.entities == entities and CLAIMS == claims
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert (
        len(payload) <= maker.MAX_CHARS
        and len(payload.encode()) <= maker.MAX_UTF8_BYTES
    )


@pytest.mark.parametrize(
    "model",
    [
        "HD120",
        "HD+130",
        "HD+ 130",
        "HD130i",
        "HD130 VV",
        "HD130-2",
        "HD130.1",
        "HD130/VV",
        "HD130 (VV)",
        "HD130, VV",
        "HD130 или HD120",
        "HD1300",
    ],
)
def test_other_models_and_suffixes_never_inherit_hd130_evidence(corpus, model):
    assert not run(corpus, COMMENT.replace("HD130", model))["sources"]
    assert not corpus.reads


@pytest.mark.parametrize(
    "comment",
    [
        "",
        "Hamm HD130",
        COMMENT.replace("Hamm ", ""),
        COMMENT.replace("Hamm", "Other"),
        "Не " + COMMENT,
        "Вместо " + COMMENT,
        COMMENT + "\nТакже Hamm HD120",
        COMMENT.replace("тандемным", "пневмоколесным"),
    ],
)
def test_comment_must_itself_bind_exact_model_to_class(corpus, comment):
    assert not run(corpus, comment)["sources"]
    assert not corpus.reads


@pytest.mark.parametrize("model", ["Hamm HD130", "HAMM HD 130", "hamm hd130."])
def test_spacing_case_and_final_punctuation_preserve_exact_model(corpus, model):
    assert run(corpus, COMMENT.replace("Hamm HD130", model))["sources"]


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [dict(CLAIMS[0], predicate="advisory")],
        [dict(CLAIMS[0], claim_id="normative:applicability")],
        [dict(CLAIMS[0], expected_text="мощность катка 100 кВт")],
        [dict(CLAIMS[0], claim_id="attribute:comment:equipment_names")],
    ],
)
def test_only_relevant_method_class_claims_are_enriched(corpus, claims):
    assert not run(corpus, claims=claims)["sources"]
    assert not corpus.reads


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_source",
        "wrong_source_sha",
        "blob_path",
        "source_kind",
        "source_pages",
        "missing_clause",
        "status",
        "text",
        "entity_source",
        "label",
        "text_metadata",
        "metadata_role",
        "metadata_sha",
        "metadata_status",
        "citation",
        "provenance_kind",
        "url",
        "html_sha",
        "html_path",
        "fact_model",
        "fact_scope",
        "fact_predicate",
        "fact_quote",
        "publisher",
    ],
)
def test_any_bad_source_or_clause_rejects_whole_two_source_set(corpus, mutation):
    expected = maker.SOURCES[1]
    item = corpus.entities[expected["clause_id"]]
    if mutation == "missing_source":
        corpus.source_index.pop(expected["source_id"])
    elif mutation in ("wrong_source_sha", "blob_path", "source_kind", "source_pages"):
        field = {
            "wrong_source_sha": "sha256",
            "blob_path": "blob_path",
            "source_kind": "kind",
            "source_pages": "pages",
        }[mutation]
        corpus.db.execute(
            "UPDATE sources SET " + field + "=? WHERE id=?",
            ("wrong", expected["source_id"]),
        )
    elif mutation == "missing_clause":
        del corpus.entities[expected["clause_id"]]
    elif mutation == "status":
        item["status"] = "unreviewed"
    elif mutation == "text":
        item["text"] += " for every layer"
    elif mutation == "entity_source":
        item["source_id"] = maker.SOURCES[0]["source_id"]
    elif mutation == "label":
        item["label"] = "other"
    elif mutation == "text_metadata":
        item["edition"]["reviewed_clauses"][0]["verification"] = "unreviewed"
    elif mutation == "metadata_role":
        item["edition"]["evidence_role"] = "automatic_approval"
    elif mutation == "metadata_sha":
        item["edition"]["sha256"] = "0" * 64
    elif mutation == "metadata_status":
        item["edition"]["status"] = "downloaded"
    elif mutation == "citation":
        item["citations"][0]["physical_page"] = 2
    elif mutation == "provenance_kind":
        item["edition"]["provenance"]["artifact_type"] = "official_factory_pdf"
    elif mutation == "url":
        item["edition"]["provenance"]["url"] = "https://other.example/"
    elif mutation == "html_sha":
        item["edition"]["provenance"]["raw_html_sha256"] = "0" * 64
    elif mutation == "html_path":
        item["edition"]["provenance"]["raw_html_blob_path"] = "other.html"
    elif mutation == "fact_model":
        item["edition"]["reviewed_facts"][0]["subject"]["model"] = "HD120"
    elif mutation == "fact_scope":
        item["edition"]["reviewed_facts"][0]["scope"] = "project_approved"
    elif mutation == "fact_predicate":
        item["edition"]["reviewed_facts"][0]["predicate"] = "approve"
    elif mutation == "fact_quote":
        item["edition"]["reviewed_facts"][0]["literal_quote"] = "invented"
    elif mutation == "publisher":
        corpus.entities[maker.SOURCES[0]["clause_id"]]["edition"]["provenance"][
            "publisher"
        ] = "unknown"
    result = run(corpus)
    assert (
        result["sources"] == result["preferred_source_ids"] == []
        and result["context"] == {}
    )


@pytest.mark.parametrize("field", ["sha256", "release_id", "database", "profile"])
def test_wrong_expected_pin_is_rejected(corpus, field):
    assert (
        run(corpus, pin=dict(PIN, **{field: "other"}))["reason"]
        == "corpus_pin_mismatch"
    )
    assert not corpus.reads


def test_future_additive_release_with_same_verified_sources_is_allowed(corpus):
    corpus.identity = dict(
        PIN,
        release_id="home-additive",
        database="releases/additive/knowledge.sqlite",
        sha256="a" * 64,
    )
    assert run(corpus)["context"]["kb"] == corpus.identity
    corpus.source_index.clear()
    assert not run(corpus)["sources"]


def test_identity_change_mid_read_fails_closed(corpus):
    original = corpus.entity

    def changing(kind, key):
        value = original(kind, key)
        corpus.identity = dict(PIN, sha256="b" * 64)
        return value

    corpus.entity = changing
    assert run(corpus)["reason"] == "corpus_pin_changed"


@pytest.mark.parametrize("file_kind", ["pdf", "html"])
def test_missing_or_changed_original_file_rejects_context(
    corpus, monkeypatch, file_kind
):
    monkeypatch.setattr(
        maker,
        "_verified_file",
        lambda _root, relative, _sha: not relative.endswith("." + file_kind),
    )
    assert not run(corpus)["sources"]


def test_budgets_fail_closed(corpus, monkeypatch):
    assert not run(
        corpus, claims=[dict(CLAIMS[0], claim_id=f"method:{i}") for i in range(5)]
    )["sources"]
    monkeypatch.setattr(maker, "MAX_UTF8_BYTES", 10)
    assert run(corpus)["reason"] == "manufacturer_context_budget_exceeded"


def test_file_verification_reads_bytes_and_checks_path_boundaries(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"literal source bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert maker._verified_file(tmp_path, "source.pdf", digest)
    assert not maker._verified_file(tmp_path, "source.pdf", "0" * 64)
    assert not maker._verified_file(tmp_path, "absent.pdf", digest)
    assert not maker._verified_file(tmp_path / "child", "../source.pdf", digest)


def test_full_literals_survive_source_compaction_and_alias_roundtrip(corpus):
    from normative_agent import compact, model_source_aliases

    result = run(corpus)
    sources = [
        dict(id="p", kind="project", text="Проектная конструкция"),
        *result["sources"],
    ]
    supplied = compact(
        dict(manufacturer_context=result["context"], other="x" * 38000),
        sources,
        result["preferred_source_ids"],
    )
    assert supplied["manufacturer_context"] == result["context"]
    model_input, aliases = model_source_aliases(supplied, sources)
    for original, sent in zip(
        result["context"]["literal_clauses"],
        model_input["manufacturer_context"]["literal_clauses"],
        strict=True,
    ):
        assert sent["quote"] == original["quote"]
        assert aliases[sent["source_id"]] == original["source_id"]


def test_same_class_on_another_machine_does_not_inherit_hamm_claim(corpus):
    other = 'Уплотнение покрытия производится тандемным виброкатком'
    claims = [*deepcopy(CLAIMS), dict(CLAIMS[0], claim_id='method:comment:equipment:2', expected_text=other)]
    comment = COMMENT + '\n' + other + ' Bomag BW161'
    result = run(corpus, comment, claims)
    assert result['context']['claim_ids'] == [CLAIMS[0]['claim_id']]
    assert not run(corpus, comment, claims[1:])['sources']


def test_duplicate_functional_claims_are_ambiguous_and_get_no_context(corpus):
    claims = [*deepcopy(CLAIMS), dict(CLAIMS[0], claim_id='method:comment:equipment:2')]
    assert not run(corpus, claims=claims)['sources']


@pytest.mark.parametrize('expected', [
    'Уплотнение покрытия производится тандемным виброкатком',
    'Уплотнение основания производится тандемным виброкатком мощностью 100 кВт',
    'Уплотнение основания производится тандемным виброкатком массой 13 т',
])
def test_different_scope_or_extra_parameters_cannot_expand_class_evidence(corpus, expected):
    assert not run(corpus, claims=[dict(CLAIMS[0], expected_text=expected)])['sources']
