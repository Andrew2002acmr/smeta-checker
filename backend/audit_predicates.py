"""Bounded VOR predicates and source-backed checks; no model verdicts or NLP."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from audit_measurements import compare_measurements, iter_measurements, parse_measurement
from audit_models import ClaimCheck, ClaimKind, ClaimSpec, ClaimState, EvidenceFact

_PROPERTIES = {
    "density": r"плотност\w*",
    "strength": r"прочност\w*|разрывн\w*\s+нагруз\w*",
    "thickness": r"толщин\w*",
    "fraction": r"фракци\w*",
    "length": r"длин\w*",
    "width": r"ширин\w*",
    "height": r"высот\w*",
    "area": r"площад[ьи]\w*",
    "consumption": r"расход\w*|норм\w*",
    "load": r"нагруз\w*",
    "overlap": r"нахлест\w*|нахлёст\w*",
}
_LABEL_RE = re.compile("|".join(f"(?P<{key}>{value})" for key, value in _PROPERTIES.items()), re.I)
_STANDARD_RE = re.compile(r"\b(?:ГОСТ|СП|ТУ)\s*[\d][\d.\-–]*(?:\s*[-–]\s*\d+)?", re.I)
_CODE_RE = re.compile(r"(?<!\w)(?:М\s*\d{2,4}|В\s*\d{1,3}|ПБЗГУ[®\-\s]*\d+|ГБП[-–\d\wА-Яа-яЁё]+|Т[-–]\d+[-–][А-ЯA-Z]+|DIN\s*\d+|Акер)(?!\w)", re.I)


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е")).strip()


def _designation_text(text):
    # ГОСТ 977-88, ГОСТ-977-88 and a line break after ГОСТ- are the
    # same designation; retain every digit and the actual standard number.
    text = normalize(text)
    text = re.sub(r"\b(гост|сп|ту)\s*[-–—]?\s*(?=\d)", r"\1 ", text)
    return re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", "-", text)


def _measurement(text):
    atoms = iter_measurements(text)
    if len(atoms) != 1:
        return parse_measurement(text)
    atom = atoms[0]
    operator = re.search(r"(?:не\s+менее|не\s+более|[≥≤<>]=?)", text, re.I)
    return parse_measurement((operator.group() + " " if operator else "")
                             + str(atom.value), unit=atom.unit)


def technical_specs(name, comment, position_type, cells, formula=""):
    """Extract only explicit property spans; unknown technical clauses stay open."""
    result = []
    for origin, text in (("name", name), ("comment", comment)):
        if not text:
            continue
        labels = list(_LABEL_RE.finditer(text))
        covered = []
        for number, label in enumerate(labels):
            end = labels[number + 1].start() if number + 1 < len(labels) else len(text)
            clause = text[label.start():end].strip(" ,;.\n")
            prop = label.lastgroup
            tail = clause[label.end() - label.start():].strip(" :=,()")
            measurements = iter_measurements(tail)
            kind = (ClaimKind.GEOMETRY if prop in {"thickness", "length", "width", "height", "area"}
                    else ClaimKind.BASIS if prop == "consumption" else ClaimKind.ATTRIBUTE)
            directed = re.search(r"\d+(?:[.,]\d+)?\s*/\s*\d+(?:[.,]\d+)?", tail)
            variant = re.search(r"\d+\s*\(\s*\d+\s*\)\s*[-–—]\s*\d+", tail)
            if directed:
                value, predicate = directed.group(), "directed_pair"
            elif variant:
                # A variant designation (e.g. fraction 5 (3)–20) is a label,
                # not arithmetic: it closes on verbatim presence, like a code.
                value, predicate = variant.group(), "designation"
            elif len(measurements) == 1:
                raw = measurements[0].raw
                # Keep a comparison operator immediately before the numeric span.
                prefix = tail[:tail.find(raw)].strip()
                operator = re.search(r"(?:не\s+менее|не\s+более|[≥≤<>]=?)\s*$", prefix, re.I)
                value = (operator.group(0) + " " if operator else "") + raw
                predicate = "measurement"
            else:
                value, predicate = tail, "unsupported"
            result.append(ClaimSpec(
                claim_id=f"{kind.value}:{origin}:{prop}:{number + 1}", kind=kind,
                label=clause, cells=cells, expected_text=value,
                predicate=predicate, property_label=prop, required=True))
            covered.append((label.start(), end))
        for regex, predicate in ((_STANDARD_RE, "designation"), (_CODE_RE, "designation")):
            for match in regex.finditer(text):
                result.append(ClaimSpec(
                    claim_id=f"attribute:{origin}:designation:{match.start()}",
                    kind=ClaimKind.ATTRIBUTE, label=match.group(), cells=cells,
                    expected_text=match.group(), predicate=predicate, required=True))
        if origin == "name" and re.search(r"\d", text) and not labels and not any(
                spec.claim_id.startswith("attribute:name:") for spec in result):
            # Last resort: digits the bounded parser cannot classify (neither
            # property, nor code, nor measurement). Advisory, not a blocker:
            # presence/quantity/unit still decide the verdict.
            result.append(ClaimSpec(
                claim_id="attribute:name:unparsed", kind=ClaimKind.ATTRIBUTE,
                label=text, cells=cells, expected_text=text,
                predicate="advisory", required=False))
        # A nonempty technical comment cannot disappear just because the bounded
        # parser does not understand it. Plain informational notes are advisory.
        if origin == "comment" and not labels and not any(s.claim_id.startswith("attribute:comment") for s in result):
            technical = bool(re.search(r"\d|соедин|креплен|уклад|засып|уплотн|вручн|материал|анкер|геотекст|бетон", text, re.I))
            result.append(ClaimSpec(
                claim_id="attribute:comment:condition", kind=ClaimKind.ATTRIBUTE,
                label=text, cells=cells, expected_text=text,
                predicate="text" if technical else "advisory", required=technical))
    if re.search(r"\d\s*[*×]\s*\d", formula) and any(s.kind == ClaimKind.GEOMETRY for s in result):
        result.append(ClaimSpec(claim_id="basis:measurement", kind=ClaimKind.BASIS,
                               label="Методика обмера: геометрия не равна измеренному объёму работ",
                               cells=cells, predicate="unsupported", required=True))
    return result


def predicate_check(spec, evidence):
    """No implicit minimum or equivalence. Ambiguous layout never confirms."""
    facts = [EvidenceFact(claim_id=spec.claim_id, value=item["quote"],
                          source_ids=[item["source_id"]],
                          locators=[item.get("locator", {})]) for item in evidence]
    def outcome(state, reason):
        return ClaimCheck(claim_id=spec.claim_id, kind=spec.kind, state=state,
                          reasons=[reason], facts=facts, required=spec.required)
    if spec.predicate == "advisory":
        return outcome(ClaimState.NOT_APPLICABLE, "informational_note")
    if spec.predicate == "unsupported" or spec.kind == ClaimKind.BASIS:
        return outcome(ClaimState.REVIEW_REQUIRED, "unsupported_predicate" if spec.kind != ClaimKind.BASIS else "measurement_basis_unresolved")
    if not evidence:
        return outcome(ClaimState.NOT_FOUND, "no_proposal")
    if any(item.get("locator", {}).get("quality", "usable") != "usable" for item in evidence):
        return outcome(ClaimState.REVIEW_REQUIRED, "unusable_ocr")
    if spec.predicate in {"text", "designation"}:
        normalizer = _designation_text if spec.predicate == "designation" else normalize
        expected = normalizer(spec.expected_text)
        if any(expected in normalizer(item["quote"]) for item in evidence):
            return outcome(ClaimState.CONFIRMED, "verbatim_predicate")
        return outcome(ClaimState.REVIEW_REQUIRED, "equivalence_unproven")
    if spec.predicate == "directed_pair":
        # A missing operator is not a minimum. Material applicability requires
        # an engineering basis even when another product has larger figures.
        expected = normalize(spec.expected_text).replace(" ", "")
        if any(expected in normalize(item["quote"]).replace(" ", "") for item in evidence):
            return outcome(ClaimState.CONFIRMED, "directed_pair_equal")
        return outcome(ClaimState.REVIEW_REQUIRED, "requirement_operator_unresolved")
    expected = _measurement(spec.expected_text)
    applicable = []
    for item in evidence:
        locator = item.get("locator", {})
        quote = item["quote"]
        # A structured OCR cell must supply its own row and column labels.
        if locator.get("origin") == "ocr" and not (locator.get("row_id") and locator.get("column_id") and locator.get("header_ids")):
            continue
        property_re = _PROPERTIES.get(spec.property_label)
        labels = list(re.finditer(property_re, quote, re.I)) if property_re else []
        if not labels:
            continue
        for label in labels:
            tail = quote[label.end():]
            next_label = _LABEL_RE.search(tail)
            if next_label:
                tail = tail[:next_label.start()]
            measures = iter_measurements(tail)
            if len(measures) == 1:
                applicable.append(measures[0])
    distinct = {(str(item.value), item.unit) for item in applicable}
    if len(distinct) > 1:
        return outcome(ClaimState.SOURCE_CONFLICT, "conflicting_applicable_facts")
    if len(applicable) != 1 and not (applicable and len(distinct) == 1):
        return outcome(ClaimState.REVIEW_REQUIRED, "layout_unresolved")
    actual = applicable[0]
    # Both values are parsed through the same unit conversion; atomic iterator
    # stores source units, parse_measurement converts into canonical dimensions.
    actual = parse_measurement(str(actual.value), unit=actual.unit)
    if spec.kind == ClaimKind.GEOMETRY:
        expected = replace(expected, kind="geometry")
        actual = replace(actual, kind="geometry")
    comparison = compare_measurements(expected, actual)
    return outcome(ClaimState(comparison["state"]), comparison["reason"])


def geometry_product(specs, checks, grouped, scope):
    """Prove S×t independently; never promote it to measured work quantity."""
    by_id = {check.claim_id: check for check in checks}
    operands = {}
    source_ids = []
    locators = []
    pages = set()
    for spec in specs.values():
        if spec.property_label not in {"area", "thickness"}:
            continue
        check = by_id.get(spec.claim_id)
        if check is None or check.state != ClaimState.CONFIRMED:
            continue
        items = grouped.get(spec.claim_id, [])
        for item in items:
            match = re.search(_PROPERTIES[spec.property_label], item["quote"], re.I)
            if match is None:
                continue
            fact = _measurement(item["quote"][match.end():])
            if fact.status != "exact" or fact.value is None:
                continue
            operands[spec.property_label] = fact.value
            pages.add(item["page"])
            source_ids.append(item["source_id"])
            locators.append(item.get("locator", {}))
    if set(operands) != {"area", "thickness"}:
        return None
    if len(pages) > 1 and not scope:
        return ClaimCheck(claim_id="geometry:product", kind=ClaimKind.GEOMETRY,
                          state=ClaimState.REVIEW_REQUIRED, required=True,
                          reasons=["measurement_subject_unresolved"])
    return ClaimCheck(
        claim_id="geometry:product", kind=ClaimKind.GEOMETRY,
        state=ClaimState.CONFIRMED, required=False,
        reasons=["geometry_only_not_measurement_basis"],
        facts=[EvidenceFact(
            claim_id="geometry:product",
            value=format(operands["area"] * operands["thickness"], "f"),
            unit="м3", source_ids=list(dict.fromkeys(source_ids)),
            locators=locators)])
