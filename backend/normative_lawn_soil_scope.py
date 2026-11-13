"""A local lawn/topsoil identity bridge, never a norm or verdict decision."""

import re

from normative_parameters import clean, measure, quoted, unit_quoted
from normative_thickness_rules import base_thickness, definition_text


LAWN = r"\bгазон[а-яё]*\b"
EARTH = r"\bрастительн[а-яё]*\s+земл[а-яё]*\b"
FERTILE_LAYER = r"\bплодородн[а-яё]*\s+сло[йя]\s+почв[а-яё]*\b"
OTHER = re.compile(
    r"[±≈~]|"
    r"\b(?:не|без|кроме|либо|или|вместо|вручную|около|примерно|приблизительно|плюс|минус)\b|\bручн[а-яё]*\b|"
    r"\b(?:сняти|срезк|выемк|разборк|замен|дренаж|асфальт|бетон|щеб|щпс|песок|песк|песчан|тротуар)[а-яё]*",
    re.I,
)


def _column_thickness(text):
    """Read one explicit numeric length; tolerate only literal Latin cm/mm/m."""
    literals = list(
        re.finditer(
            r"(?<![\w.,])(\d+(?:[.,]\s*\d+)?)\s*(мм|см|м|mm|cm|m)(?![\w²³])", text, re.I
        )
    )
    if len(literals) != 1:
        return None
    literal = literals[0]
    left, right = text[: literal.start()], text[literal.end() :]
    # A bound/range endpoint cannot become an exact layer height. Accept only
    # a literal parenthesized header or an explicit h=/thickness label.
    if (
        not re.search(
            r"(?:\(|\bh\s*=\s*|\bтолщин[а-яё]*(?:\s+слоя)?\s+)\s*$", left, re.I
        )
        or re.search(
            r"(?:[<>≤≥≈~]|\b(?:от|до|более|менее|свыше|минимум|максимум))[^;|:\n]{0,30}$",
            left,
            re.I,
        )
        or left.rstrip().endswith("(")
        and not right.lstrip().startswith(")")
        or re.match(r"\s*\)*\s*(?:[-–—<>≤≥≈~±+−]|\b(?:до|и|или)\s*\d)", right, re.I)
    ):
        return None
    unit = literal[2].casefold()
    return measure(literal[1], {"cm": "см", "mm": "мм", "m": "м"}.get(unit, unit))


def lawn_soil_scope(position, result, cards, fact, definition):
    """Bridge only explicit lawn soil placement, using the current card sources.

    The caller still requires exact operation thickness, a selected literal
    measure and a separately proved signed adjustment. No source is assigned,
    and no saved/model-supplied proof flag authorizes this bridge.
    """
    work = clean(position.work_name)
    section = getattr(getattr(position, "position_source", None), "sections", [])
    section_text = clean(section[-1].text) if section else ""
    base = clean(definition_text(definition["text"]))
    scope = clean(fact["scope_quote"])
    heights = re.findall(r"\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b", work)
    if (
        not re.search(r"\bустройств[а-яё]*\s+" + LAWN, work)
        or not re.search(r"\bвнесени[а-яё]*\s+" + EARTH, work)
        or not re.search(r"\bмеханизирован[а-яё]*\b", work)
        or not re.search(LAWN, section_text)
        or not re.search(
            r"\bподготовк[а-яё]*\s+почв[а-яё]*\s+для\s+устройств[а-яё]*\b[^.:;]{0,120}"
            + LAWN
            + r"\s+с\s+внесени[а-яё]*\s+"
            + EARTH,
            base,
        )
        or not base_thickness(base)
        or not re.search(
            re.escape(definition["code"]) + r"\s+механизированным\s+способом\b", base
        )
        or re.search(r"\b(?:вручную|ручн[а-яё]*)\b", work + " " + base)
        or not re.search(FERTILE_LAYER + r"|\bсло[йя]\s+" + EARTH, scope)
        or len(scope) > 600
        or len(heights) != 1
        or measure(*heights[0]) != fact["value"]
        or any(OTHER.search(text) for text in (work, section_text, base, scope))
    ):
        return False
    thickness = base_thickness(re.sub(r"(?<=\d)([.,])\s+(?=\d)", r"\1", scope))
    if not thickness or thickness["value"] != fact["value"][1]:
        return False
    sources = {s["id"]: s for s in result["sources"]}
    project = sources.get(fact["source_id"], {})
    if (
        project.get("origin") != "native"
        or not project.get("version_id")
        or not project.get("page")
    ):
        return False
    quantity = next(
        (c for c in cards if c["id"] == "quantity" and c["state"] == "confirmed"), None
    )
    proposals = [
        p
        for p in result.get("parameter_proposals", [])
        if p.get("parameter") == "quantity"
    ]
    if not quantity or len(proposals) != 1:
        return False
    proposal = proposals[0]
    source = sources.get(proposal.get("source_id"), {})
    qscope = clean(proposal.get("scope_quote", ""))
    value = measure(proposal.get("value"), proposal.get("unit"))
    return bool(
        source.get("kind") == "project"
        and source.get("version_id") == project["version_id"]
        and source.get("page")
        and ":cell:" in source.get("id", "")
        and source.get("rects")
        and source.get("numeric_verified") is True
        and source.get("unit_verified") is True
        and (source.get("origin") == "native" or source.get("origin") == "ocr")
        and source["id"] in quantity.get("source_ids", [])
        and proposal.get("scope_state") == "verified"
        and proposal.get("operation", "direct") == "direct"
        and value is not None
        and value[0] == "area"
        and value == measure(quantity.get("found_value"), quantity.get("found_unit"))
        and value == measure(position.volume, position.unit)
        and value
        == measure(
            source.get("text", "").strip().rsplit("\n", 1)[-1], source.get("unit")
        )
        and measure("1", source.get("unit")) == measure("1", proposal.get("unit"))
        and quoted(proposal.get("value"), proposal.get("quote", ""), qscope)
        and unit_quoted(proposal.get("unit", ""), proposal.get("quote", ""))
        and qscope in clean(source.get("text", ""))
        and _column_thickness(qscope) == fact["value"]
        and _column_thickness(clean(source.get("text", ""))) == fact["value"]
        and re.search(LAWN, qscope)
        and re.search(
            r"\bпочвенно[\s-]*растительн[а-яё]*\b|" + EARTH + "|" + FERTILE_LAYER,
            qscope,
        )
        and not OTHER.search(qscope)
    )


def _lawn_categories(text):
    """Literal adjective-to-lawn relation, never meadow grass or soil."""
    stems=r'(?:лугов|обыкновенн|партерн|спортивн|мавританск)[а-яё]*'
    bound=[m[0] for m in re.finditer(r'\b'+stems+r'\s+газон[а-яё]*\b',text)]
    bound += [m[0] for m in re.finditer(r'\bгазон[а-яё]*\s+'+stems+
        r'(?:(?:\s*,\s*|\s+и\s+)'+stems+r')*',text)]
    return set(stem for phrase in bound for stem in re.findall(
        r'\b(лугов|обыкновенн|партерн|спортивн|мавританск)[а-яё]*\b',phrase))


def areal_resource_scope_gap(definition, project_scopes):
    """Do not widen a qualified lawn recipe through equal resource doses.

    Only the verified scope of this recipe's project area supplies a category
    or a manual operation. A machine classification or a green model item is
    not a literal project lawn category.
    """
    title=clean(definition.split('Состав работ:')[0])
    categories=_lawn_categories(title)
    scopes=[clean(scope) for scope in project_scopes if scope]
    positive=[scope for scope in scopes if not re.search(r'\b(?:не|без|кроме|либо|или|вместо)\b',scope)
        and not re.search(r'\b(?:друг|ин|соседн)[а-яё]*\s+участ[а-яё]*\b',scope)]
    if categories:
        found=set(stem for scope in positive for stem in _lawn_categories(scope))
        if len(found)!=1 or not found<=categories:
            return ('Буквальное определение нормы ограничено категорией газона «'+', '.join(sorted(categories))+
                '…»; положительная проектная привязка допустимой категории к площади расчёта отсутствует. '
                'Одинаковый расход и подтверждение класса машины не расширяют область нормы.')
    manual=r'\b(?:вручную|ручн[а-яё]*)\b'
    mechanical=r'\b(?:механизирован[а-яё]*|трактор[а-яё]*|сеялк[а-яё]*)\b'
    if re.search(manual,title):
        if not any(re.search(manual,scope) and not re.search(mechanical,scope) for scope in positive):
            return ('Норма задаёт ручной способ; согласованная положительная привязка этого '
                'способа к текущей рецептуре не установлена. Равный расход не доказывает применимость способа.')
    if re.search(r'\bсеялк[а-яё]*\b',title):
        tractor=bool(re.search(r'\bтрактор[а-яё]*\b',title))
        if not any(re.search(r'\bсеялк[а-яё]*\b',scope) and not re.search(manual,scope)
                and (not tractor or re.search(r'\bтрактор[а-яё]*\b',scope)) for scope in positive):
            if any(re.search(manual,scope) for scope in positive):
                return 'Проектная привязка площади явно задаёт ручной способ, несовместимый с посевом сеялкой выбранной нормы.'
            return ('Норма задаёт посев '+('тракторной ' if tractor else '')+
                'сеялкой; связанное положительное основание способа/функции машины для этой рецептуры не установлено. '
                'Этот ограниченный числовой маршрут не подтверждает связь дополнения ВОР с классом машины; '
                'буквальное перечисление машины в ПД не является обязательным требованием.')
    return None
