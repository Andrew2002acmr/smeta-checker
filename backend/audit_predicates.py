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
_CODE_RE = re.compile(r"(?<!\w)(?:[МM]\s*\d{2,4}|В\s*\d{1,3}|ПБЗГУ[®\-\s]*\d+|ГБП[-–\d\wА-Яа-яЁё]+|Т[-–]\d+[-–][А-ЯA-Z]+|DIN\s*\d+|Акер)(?!\w)", re.I)
_INDEPENDENT_NOTE = re.compile(r'(?:\n|;)[ \t]*(?=(?:Устройство|Уплотнение|Укладка|Монтаж|Демонтаж|'
    r'Расход|Коэффициент|Плотность|Мощность|Температура|Требуется|Необходимо)\b)',re.I)


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


def _machine_name_clause(part):
    """Split only a complete action/class/name clause with no engineering limits."""
    if (iter_measurements(part) or re.search(r'типоразмер|мощност|температур|производительност|'
            r'ширин|глубин|скорост|масс|вес|давлен|частот|амплитуд|проход|'
            r'обязател|только|не\s+менее|не\s+более|ГОСТ|ГЭСН|\d\s*(?:кВт|°|%|кПа)',part,re.I)):
        return None
    match=re.fullmatch(r'((?:Уплотнение|Укладка)(?:\s+(?:основания|покрытия|смеси))?'
        r'(?:\s+производится)?\s+(?:(?:тандемным|самоходным)\s+)?'
        r'(?:виброкатком|катком|асфальтоукладчиком|а/укладчиком))\s+(.+)',part)
    if (not match or not re.search(r'\d',match[2])
            or re.search(r'\b(?:при|без|с|не|и|или|для)\b',match[2],re.I)):
        return None
    # Every designation token must be a name/model token. Lowercase prose,
    # punctuation and engineering clauses are not silently discarded.
    tokens=match[2].split()
    if not all(re.fullmatch(r'[^\W\d_][\w.–-]*|\d[\w.–-]*',token)
            and (token[0].isupper() or token[0].isdigit()) for token in tokens):
        return None
    if any(any(char.islower() for char in token) for token in tokens[1:]):
        return None
    return match[1]


def comment_machine_clauses(text):
    """Keep every unknown/technical clause; separate literal machine-name lines."""
    retained=[]
    machines=[]
    for raw in re.split(r'\n+|;\s*',text):
        clause=raw.strip(' .;')
        method=_machine_name_clause(clause)
        if method is None:
            retained.append(raw)
        else:
            machines.append((clause,method))
    return '\n'.join(retained),machines


def comment_metadata(text):
    """Recognize only complete metadata/name-only clauses, never engineering limits."""
    parts=[p.strip(' .;') for p in re.split(r'\n+|;\s*',text) if p.strip(' .;')]
    equipment=[]
    if not parts:
        return None
    for part in parts:
        # Units, technical constraints and unknown clauses keep their old gate.
        if (iter_measurements(part) or re.search(r'ГОСТ|ГЭСН|СНиП|\bСП\s*\d|\bТУ\s*\d|'
                r'типоразмер|мощност|температур|производительност|ширин|глубин|скорост|'
                r'обязател|только|не\s+менее|не\s+более|\d\s*(?:кВт|°|%|кПа)',part,re.I)):
            return None
        software=re.fullmatch(r'(?:Автоматизированная\s+система\s+расч[её]та|'
            r'Расч[её]т(?:\s+выполнен)?|Проектирование(?:\s+выполнено)?)\s+'
            r'программным\s+(?:продуктом|комплексом)\s+(.+)',part,re.I)
        if (software and all(re.fullmatch(r'[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9.–-]*|\d[\w.–-]*',token)
                for token in software[1].strip(' "«»').split())):
            continue
        machine=_machine_name_clause(part)
        if machine is not None:
            equipment.append(machine)
            continue
        model=re.fullmatch(r'((?:Агрегат|Машина|Установка)\s+для\s+[а-яё]+(?:\s+[а-яё]+){0,2})'
            r'\s+([A-ZА-ЯЁ][\w .–-]*\d[\w .–-]*)',part)
        if (not model or re.search(r'\b(?:при|без|с|не)\b',model[2],re.I)
                or any(not re.fullmatch(r'[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9.–-]*|\d[\w.–-]*',token)
                    for token in model[2].split())):
            return None
        equipment.append(model[1])
    return equipment


def typed_paver_comment(name, comment):
    """Separate a model only when one literal paver class is fully retained.

    Engineering tails are deliberately outside this grammar. A disagreement
    with the class in the work name keeps the original comment gate intact.
    """
    clause = comment.strip(' .;')
    match = re.fullmatch(
        r'(?P<named>Укладка\s+(?:а/укладчиком|асфальтоукладчиком)\s+.+)\s+'
        r'(?P<class>[123])(?P<ending>[-‑–]?(?:го|ого|й))?\s+типоразмера', clause)
    if (not match or '\n' in clause or ';' in clause
            or re.search(r'\d\s*[-‑–—]\s*$', match['named'])):
        return None
    method = _machine_name_clause(match['named'])
    if method is None:
        return None
    if re.search(r'типоразмер', name, re.I):
        declared = list(re.finditer(
            r'(?:асфальтоукладчиком|а/укладчиком)\s+([123])'
            r'(?:[-‑–]?(?:го|ого|й))?\s+типоразмера\b', name, re.I))
        if (len(declared) != 1 or len(re.findall('типоразмер', name, re.I)) != 1
                or declared[0][1] != match['class']
                or re.search(r'\b(?:не|без|кроме|либо|или|только)\b', name, re.I)):
            return None
    return method + ' ' + match['class'] + (match['ending'] or '') + ' типоразмера'


def paver_class_condition(comment):
    return bool(re.search(r'Укладка\s+(?:а/укладчиком|асфальтоукладчиком)\b', comment)
        and re.search(r'типоразмер', comment, re.I))


def technical_specs(name, comment, position_type, cells, formula=""):
    """Extract only explicit property spans; unknown technical clauses stay open."""
    result = []
    for origin, text in (("name", name), ("comment", comment)):
        if not text:
            continue
        typed_method = typed_paver_comment(name, text) if origin == 'comment' else None
        if typed_method is not None:
            result.append(ClaimSpec(claim_id='attribute:comment:condition', kind=ClaimKind.ATTRIBUTE,
                label=text, cells=cells, expected_text=text, predicate='advisory', required=False))
            result.append(ClaimSpec(claim_id='method:comment:equipment:1', kind=ClaimKind.METHOD,
                label=typed_method, cells=cells, expected_text=typed_method, predicate='text',
                property_label='equipment_function',required=True))
            continue
        if origin == 'comment' and paver_class_condition(text):
            # A later width/power predicate must not swallow the class or an
            # unknown qualifier before it. Preserve the full unresolved clause.
            result.append(ClaimSpec(claim_id='attribute:comment:condition', kind=ClaimKind.ATTRIBUTE,
                label=text, cells=cells, expected_text=text, predicate='text', required=True))
        metadata=comment_metadata(text) if origin=='comment' else None
        if metadata is not None:
            result.append(ClaimSpec(claim_id='attribute:comment:condition',kind=ClaimKind.ATTRIBUTE,
                label=text,cells=cells,expected_text=text,predicate='advisory',required=False))
            for number,equipment in enumerate(metadata,1):
                result.append(ClaimSpec(claim_id=f'method:comment:equipment:{number}',kind=ClaimKind.METHOD,
                    label=equipment,cells=cells,expected_text=equipment,predicate='text',
                    property_label='equipment_function',required=True))
            continue
        if origin=='comment':
            remainder,machines=comment_machine_clauses(text)
            if machines:
                result.append(ClaimSpec(claim_id='attribute:comment:equipment_names',kind=ClaimKind.ATTRIBUTE,
                    label='\n'.join(clause for clause,_ in machines),cells=cells,
                    expected_text='\n'.join(clause for clause,_ in machines),predicate='advisory',required=False))
                for number,(_,method) in enumerate(machines,1):
                    result.append(ClaimSpec(claim_id=f'method:comment:equipment:{number}',kind=ClaimKind.METHOD,
                        label=method,cells=cells,expected_text=method,predicate='text',
                        property_label='equipment_function',required=True))
                text=remainder
        labels = list(_LABEL_RE.finditer(text))
        covered = []
        separated_notes = []
        for number, label in enumerate(labels):
            end = labels[number + 1].start() if number + 1 < len(labels) else len(text)
            boundary=_INDEPENDENT_NOTE.search(text,label.end(),end)
            if boundary:
                # A new physical note is not part of the preceding property.
                # Keep it as a separate mandatory condition, not discarded text.
                separated_notes.append(text[boundary.end():end].strip())
                end=boundary.start()
            clause = text[label.start():end].strip(" ,;.\n")
            prop = label.lastgroup or ''
            tail = clause[label.end() - label.start():].strip(" :=,()\n\r\t")
            measurements = iter_measurements(tail)
            if prop=='area' and len(measurements)==1 and measurements[0].dimension!='area':
                height=re.search(r'(?<!\w)h\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',tail,re.I)
                if height and measurements[0].dimension=='length':
                    # "планировка площади ... h=0,20 м" describes a layer,
                    # not an area of 0.20 metres. Keep the explicit h requirement.
                    prop='thickness'
                    clause=height[0]
                    tail=height[1]+' '+height[2]
                    measurements=iter_measurements(tail)
                else:
                    # An explicit but dimensionally invalid area stays open.
                    measurements=[]
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
            normative_instruction=prop=='consumption' and re.fullmatch(r'нормир\w*',label.group(),re.I)
            if normative_instruction:
                # Retain the legacy claim handle for frozen reviews. Its exact
                # semantic definition, not that opaque handle, controls the task.
                value,predicate=tail,'text'
            result.append(ClaimSpec(
                claim_id=f"{kind.value}:{origin}:{prop}:{number + 1}", kind=kind,
                label=clause, cells=cells, expected_text=value,
                predicate=predicate, property_label='normative_instruction' if normative_instruction else prop, required=True))
            covered.append((label.start(), end))
        if separated_notes:
            result.append(ClaimSpec(claim_id=f'attribute:{origin}:separate_notes',kind=ClaimKind.ATTRIBUTE,
                label='\n'.join(separated_notes),expected_text='\n'.join(separated_notes),
                cells=cells,predicate='text',required=True))
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
        if origin == "comment" and not labels and not any(s.claim_id.startswith("attribute:comment")
                and s.claim_id!='attribute:comment:equipment_names' for s in result):
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
