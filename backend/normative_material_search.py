"""Property-definition retrieval and bounded mixture specification proofs.

Lexical candidates never establish equivalence. A separate literal proof may
close one independent aggregate-grade refinement, never batch acceptance.
Expected quantities and formula operands are not used as search terms.
"""

import re
import unicodedata
from copy import deepcopy
from typing import Any

TRACE = "aggregate_grade_specification_compatibility_v1"
TOPIC = "Совместимость обозначений смеси и щебня в спецификации"


STANDARD = re.compile(r"\bГОСТ\s*(?:Р\s*)?\d+(?:[.\-]\d+)*(?:\s*[-–]\s*\d{4})?", re.I)
PROPERTY = re.compile(r"(?<!\w)(?:М|M|К|K|F|В|B|W)\s*\d+(?:[.,]\d+)?(?!\w)")


def _property(text):
    return (
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
        .upper()
        .translate(str.maketrans({"М": "M", "К": "K", "В": "B"}))
    )


def search(kb, position):
    text = position.work_name + "\n" + position.comment
    standards = list(dict.fromkeys(m[0].strip() for m in STANDARD.finditer(text)))[:2]
    properties = {}
    for match in PROPERTY.finditer(text):
        properties.setdefault(_property(match[0]), match[0])
    plan = [
        (standard, prop, literal)
        for standard in standards
        for prop, literal in list(properties.items())[:3]
    ][:4]
    output, seen, queries, pools = [], set(), [], []
    for standard, prop, literal in plan:
        query = standard + " " + literal
        hits = kb.search(query, kind="GOST", facts={}, limit=3)
        selected = []
        pool = []
        for hit in hits:
            # FTS can return only the standard's other query terms. Report the
            # absence of the requested literal instead of calling it a hit.
            literals = {
                _property(m[0]) for m in PROPERTY.finditer(hit.get("text") or "")
            }
            if prop not in literals:
                continue
            selected.append(hit["id"])
            pool.append(hit)
        pools.append(pool)
        queries.append(
            dict(
                query=query,
                property=prop,
                candidate_ids=selected,
                status="literal_candidates"
                if selected
                else "no_literal_in_top_candidates",
            )
        )
    # The first property must not fill the source budget before the others.
    for rank in range(3):
        for pool in pools:
            if rank >= len(pool):
                continue
            hit = pool[rank]
            key = (hit["entity_kind"], hit["id"])
            if key not in seen and len(output) < 4:
                output.append(hit)
                seen.add(key)
    return dict(
        queries=queries,
        requires_applicability_check=True,
        warning="Разные обозначения свойств не эквивалентны без отдельного основания; совпадение марки не доказывает пригодность материала.",
    ), output


def definition_sources(kb, position):
    """Retrieve property definitions even when they omit a particular M1 token.

    The standard explicitly cited by this mixture refers aggregate properties
    to 32703. This is a bounded document-reference traversal, not equivalence.
    """
    from vor_material_links import _family

    if (
        _family(position.work_name) != "щпс"
        or not re.search(r"\bГОСТ\s+Р\s+70458-2022\b", position.comment, re.I)
        or not any(
            re.fullmatch(r"M\d{3,4}", _property(m[0]))
            for m in PROPERTY.finditer(position.work_name)
        )
    ):
        return []
    from normative_service import candidate
    from normative_source_registry import full_text_handle

    output = []
    for query, heading in [
        ("ГОСТ Р 70458-2022 5.2", "5.2"),
        ("ГОСТ Р 70458-2022 5.9", "5.9"),
        ("ГОСТ 32703-2014 5.6", "5.6"),
    ]:
        hits = kb.search(query, kind="ALL", facts={}, limit=1)
        if not hits or not re.search(
            r"(?:^|\n)\s*" + re.escape(heading) + r"(?![\d.])\s", hits[0]["text"]
        ):
            continue
        item = candidate(kb, hits[0], {})
        if not item["citations"]:
            continue
        # The grade tables of 5.6 continue on its last cited page. Retain all
        # citation pages in the trace; open the page carrying those tables.
        ref = item["citations"][-1] if heading == "5.6" else item["citations"][0]
        text = item["text"]
        if heading == "5.9":
            # Preparation attached the entire raster page to this carrier.
            # The component-reference obligation is its complete native
            # paragraph, not the unrelated page-wide annex after the marker.
            text = text.partition("\n[Дополнение: физическая страница")[0]
        output.append(
            dict(
                id=full_text_handle("material:" + item["id"], text),
                kind="normative",
                source_id=ref["source_id"],
                page=ref["page"],
                label=item["designation"] + " · " + heading,
                text=text,
                origin=item["extraction_status"],
                citation_pages=sorted({r["page"] for r in item["citations"]}),
            )
        )
    return output


def _project_specification(position, result) -> dict[str, Any] | None:
    """Use an already verified physical layer; never another row's same mark."""
    from normative_parameters import clean, material_fractions
    from normative_conditional_selection import _readable
    from vor_material_links import _family

    sources = {s["id"]: s for s in result.get("sources", [])}
    ids = {
        sid
        for c in result.get("parameter_cards", [])
        if c["id"] == "thickness" and c["state"] == "confirmed"
        for sid in c.get("source_ids", [])
    }
    declared = re.search(
        r"ГОСТ\s+Р\s+70458-2022\s+категория\s+([КK]\d+)\s*,?\s+марка\s+([МM]\d+)\b",
        position.comment,
        re.I,
    )
    if not declared:
        return None
    wanted = material_fractions(position.work_name)
    for proposal in result.get("parameter_proposals", []):
        source = sources.get(proposal.get("source_id"))
        scope = proposal.get("scope_quote", "")
        text = clean(scope)
        if (
            not source
            or source["id"] not in ids
            or source.get("kind") != "project"
            or not source.get("version_id")
            or not source.get("page")
            or not _readable(source)
            or proposal.get("parameter") != "thickness"
            or proposal.get("scope_state") != "verified"
            or not text
            or text not in clean(source["text"])
            or _family(scope) != "щпс"
            or not wanted
            or material_fractions(scope) != wanted
        ):
            continue
        category, mark = map(_property, declared.groups())
        properties = {_property(m[0]) for m in PROPERTY.finditer(scope)}
        if properties == {category, mark} and re.search(
            r"гост\s+р\s+70458-2022\b", text
        ):
            return dict(
                source_id=source["id"],
                quote=scope,
                category=category,
                mark=mark,
                version_id=source["version_id"],
                page=source["page"],
            )
    return None


def specification_proof(position, result) -> dict[str, Any] | None:
    """Prove non-conflicting specification axes, never a delivered batch's tests.

    M-grade of aggregate and M/K grading of a mixture must each retain their
    meaning. A factual particle-size analysis, rock origin, Pl/B/F and other
    unrequested properties cannot be inferred from this proof.
    """
    from audit_core import build_claim_specs
    from normative_parameters import clean
    from normative_conditional_selection import _readable
    from vor_material_links import _family

    if _family(position.work_name) != "щпс":
        return None
    if re.search(
        r"\b(?:не|без|кроме|вместо|ниже|выше|менее|более|свыше|до)\s+[мm]\s*\d",
        position.work_name,
        re.I,
    ):
        return None
    grades = [
        s
        for s in build_claim_specs(position)
        if s.claim_id.startswith("attribute:name:designation:")
        and re.fullmatch(r"M\d{3,4}", _property(s.expected_text))
    ]
    if len(grades) != 1:
        return None
    spec = grades[0]
    project = _project_specification(position, result)
    if not project:
        return None
    # A project-specified strength or additional qualification requires its
    # own comparison. This narrow rule cannot decide a strength limit.
    if any(
        re.fullmatch(r"M\d{3,4}", _property(m[0]))
        for m in PROPERTY.finditer(project["quote"])
    ) or re.search(
        r"прочн|дробим|пород|пластич|водостойк|мороз|истира|радиоакт|\bпл\s*\d",
        clean(project["quote"]),
    ):
        return None
    grade = _property(spec.expected_text)
    definitions, aggregates, references = [], [], []
    for source in result.get("sources", []):
        if (
            source.get("kind") != "normative"
            or not source.get("source_id")
            or type(source.get("page")) is not int
            or source["page"] < 1
            or not _readable(source)
        ):
            continue
        text, label = clean(source.get("text", "")), clean(source.get("label", ""))
        if re.search(r"гост\s+р\s+70458-2022\b", label):
            if all(
                phrase in text
                for phrase in [
                    "готовые смеси в зависимости от гранулометрического состава делят на категории и марки",
                    "категорию готовой смеси определяют при ее рассеве",
                    "марку готовой смеси определяют при ее рассеве",
                    "размерами ячеек менее d",
                ]
            ) and {project["category"], project["mark"]} <= {
                _property(m[0]) for m in PROPERTY.finditer(source["text"])
            }:
                definitions.append(source)
            if re.search(
                r"5\.9\s+щебень\s*\(гравий\),\s*входящий в состав готовых смесей, по дробимости, морозостойкости и устойчивости\s+структуры против распадов должен соответствовать требованиям гост 32703",
                text,
            ):
                references.append(source)
        elif (
            re.search(r"гост\s+32703-2014\b", label)
            and "5.6 дробимость" in text
            and "марка по дробимости" in text
            and grade in {_property(m[0]) for m in PROPERTY.finditer(source["text"])}
        ):
            aggregates.append(source)
    if not definitions or not aggregates or not references:
        return None
    selected = [definitions[0], references[0], aggregates[0]]
    if definitions[0]["source_id"] != references[0]["source_id"]:
        return None
    return dict(
        claim_id=spec.claim_id,
        grade=grade,
        project=project,
        normative_source_ids=[s["id"] for s in selected],
        normative_dependencies=[
            dict(
                id=s["id"],
                source_id=s["source_id"],
                page=s["page"],
                citation_pages=s.get("citation_pages", [s["page"]]),
                quote=s["text"],
            )
            for s in selected
        ],
        relation="aggregate_crushability_refines_mixture_specification",
        batch_compliance_proven=False,
        equivalence_asserted=False,
    )


def apply_specification_compatibility(position, result):
    import hashlib
    import json

    output = deepcopy(result)

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    previous = [e for e in output.get("evidence_trace", []) if e.get("kind") == TRACE]
    invalid_projection = False
    # Restore our exact projection before checking dependencies again.
    for item in output.get("items", []):
        if item.get("topic") != TOPIC:
            continue
        event = next(
            (
                e
                for e in previous
                if e.get("projected_sha256") == digest(item)
                and e.get("original_sha256") == digest(e.get("original_item"))
                and e.get("original_item", {}).get("state") == "needs_facts"
                and e.get("original_item", {}).get("claim_ids") == item.get("claim_ids")
            ),
            None,
        )
        if event:
            item.clear()
            item.update(deepcopy(event["original_item"]))
        else:
            item.update(
                state="needs_facts",
                explanation="Сохранённое доказательство совместимости требует повторной проверки.",
            )
            invalid_projection = True
    output["evidence_trace"] = [
        e for e in output.get("evidence_trace", []) if e.get("kind") != TRACE
    ]
    proof = specification_proof(position, output)
    if not proof or invalid_projection:
        if previous or invalid_projection:
            output["summary"] = " ".join(
                i["explanation"]
                for i in output["items"]
                if i.get("required", True) and i["state"] != "confirmed"
            )[:2500]
        return output
    # Existing contradictions remain visible; do not reinterpret them here.
    if any(
        i.get("state") == "possible_mismatch"
        and proof["claim_id"] in i.get("claim_ids", [])
        for i in output.get("items", [])
    ):
        return output
    if (
        sum(
            proof["claim_id"] in i.get("claim_ids", []) for i in output.get("items", [])
        )
        != 1
    ):
        return output
    items = []
    for item in output.get("items", []):
        if item.get("state") != "needs_facts" or proof["claim_id"] not in item.get(
            "claim_ids", []
        ):
            items.append(item)
            continue
        original = deepcopy(item)
        remaining = [c for c in item["claim_ids"] if c != proof["claim_id"]]
        if remaining:
            items.append({**item, "claim_ids": remaining})
        projected = dict(
            topic=TOPIC,
            state="confirmed",
            required=item.get("required", True),
            claim_ids=[proof["claim_id"]],
            project_source_ids=[proof["project"]["source_id"]],
            normative_source_ids=proof["normative_source_ids"],
            explanation="ВОР сохраняет проектные категорию и марку гранулометрии "
            + proof["project"]["category"]
            + "/"
            + proof["project"]["mark"]
            + ". Дополнительная "
            + proof["grade"]
            + " относится к дробимости щебня по ГОСТ32703, "
            "на который ссылается п.5.9 ГОСТ70458; она не заменяет гранулометрию готовой смеси из п.5.2. "
            "Совместимость заявленной спецификации подтверждена; испытания фактической партии не утверждаются.",
        )
        items.append(projected)
        original_grade = {**original, "claim_ids": [proof["claim_id"]]}
        output["evidence_trace"].append(
            dict(
                kind=TRACE,
                proof=proof,
                original_item=original_grade,
                original_full_item=original,
                original_sha256=digest(original_grade),
                projected_sha256=digest(projected),
            )
        )
    output["items"] = items
    if any(i.get("topic") == TOPIC for i in items):
        gaps = [
            c.get("label", c["id"]) + ": " + c["reason"]
            for c in output.get("parameter_cards", [])
            if c.get("required", True) and c["state"] != "confirmed"
        ]
        gaps += [
            i["explanation"]
            for i in items
            if i.get("required", True) and i["state"] != "confirmed"
        ]
        output["summary"] = (
            " ".join(gaps)
            or "Совместимость обозначений материала проверена по нормативным определениям."
        )[:2500]
    return output
