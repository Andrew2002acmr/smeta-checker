"""Wire contract and independent technical checks for Codex audit conclusions."""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


Status = Literal["confirmed", "mismatch", "needs_facts", "not_applicable", "not_checked"]
DocumentKind = Literal["project", "normative", "vor"]
Direction = Literal['project', 'normative', 'quantities']
STATES = ("confirmed", "mismatch", "needs_facts", "not_applicable", "not_checked")
DIRECTIONS = ("project", "normative", "quantities")
MAX_ADDITIONAL_FINDINGS = 12
DETERMINATE = {"confirmed", "mismatch"}


def _nonblank(text: str) -> str:
    if not text.strip():
        raise ValueError("Text must contain a non-space character")
    return text


ShortText = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_nonblank)]
LongText = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_nonblank)]
SheetName = Annotated[str, Field(max_length=512)]
PositiveInt = Annotated[int, Field(ge=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Citation(ContractModel):
    document_id: ShortText
    kind: DocumentKind
    page: PositiveInt | None = Field(description="Physical PDF page number, starting at 1.")
    sheet: SheetName | None = Field(description="Workbook sheet, or an empty string for CSV.")
    row: PositiveInt | None
    quote: Annotated[str, Field(min_length=1, max_length=1500), AfterValidator(_nonblank)]
    evidence_id: ShortText | None = None
    # Populated by the server from the verified observation.
    highlight: list[float] | None = None

    @model_validator(mode="after")
    def check_address(self) -> Citation:
        if len(normalize_text(self.quote)) < 4 and (
            self.kind == 'vor' or self.evidence_id is None or _decimal(self.quote) is None
        ):
            raise ValueError("A short numeric PDF citation requires a cell evidence ID")
        if self.kind == "vor":
            if self.page is not None or self.sheet is None or self.row is None:
                raise ValueError("A VOR citation requires a worksheet name and physical row")
        elif self.page is None or self.sheet is not None or self.row is not None:
            raise ValueError("A PDF citation requires a physical page number")
        return self


class Calculation(ContractModel):
    expression: ShortText = Field(description="Arithmetic with decimal literals, + - * / and parentheses.")
    result: ShortText = Field(description="Calculated decimal value in the reported unit.")
    operand_sources: list[int] | None = Field(default=None, description="Zero-based citation index for each numeric literal in expression order.")


class MissingFacts(ContractModel):
    required: LongText
    reason: LongText
    established: LongText


class DirectionConclusion(ContractModel):
    status: Status
    parameter: ShortText
    vor_value: ShortText | None
    source_value: ShortText | None
    unit: ShortText | None
    calculation: Calculation | None
    deviation: ShortText | None = Field(description="Source value minus VOR value when comparable.")
    conclusion: LongText
    sources: Annotated[list[Citation], Field(max_length=20)]
    missing_facts: MissingFacts | None

    @model_validator(mode="after")
    def check_status(self) -> DirectionConclusion:
        if (self.missing_facts is not None) != (self.status == "needs_facts"):
            raise ValueError("Only needs_facts requires missing_facts")
        if self.status in DETERMINATE and not self.sources:
            raise ValueError("A conclusive finding requires at least one pinned citation")
        return self


class ParameterFinding(ContractModel):
    direction: Direction
    finding: DirectionConclusion


class PositionConclusion(ContractModel):
    position_index: Annotated[int, Field(ge=0)]
    sheet: SheetName
    row: PositiveInt
    work_name: LongText
    group: ShortText
    project: DirectionConclusion
    normative: DirectionConclusion
    quantities: DirectionConclusion
    parameter_findings: Annotated[list[ParameterFinding], Field(max_length=MAX_ADDITIONAL_FINDINGS)] = Field(default_factory=list)

    @classmethod
    def from_stored(cls, item: Mapping) -> PositionConclusion:
        return cls.model_validate({key: value for key, value in item.items() if key != 'schema_version'})

    def findings(self):
        yield from ((name, getattr(self, name)) for name in DIRECTIONS)
        yield from ((item.direction, item.finding) for item in self.parameter_findings)


class AuditConclusion(ContractModel):
    project_id: ShortText
    vor_version_id: ShortText
    positions: Annotated[list[PositionConclusion], Field(min_length=1)]


class AuditContractError(ValueError):
    """Error messages contain identifiers and counts suitable for run diagnostics."""

    def __init__(self, message: str, *, invalid_citations: int = 0):
        super().__init__(message)
        self.invalid_citations = invalid_citations


def output_schema() -> dict:
    """Return the required-field JSON Schema understood by structured Codex output."""
    def supported_schema(value):
        if isinstance(value, list):
            return [supported_schema(item) for item in value]
        if isinstance(value, dict):
            return {
                key: (
                    {name: supported_schema(child) for name, child in item.items()}
                    if key in {"properties", "$defs"} else supported_schema(item)
                )
                for key, item in value.items()
                if key not in {"minLength", "maxLength", "minimum", "minItems", "maxItems", "title"}
            }
        return value

    schema = supported_schema(AuditConclusion.model_json_schema())
    for name in ('Citation', 'Calculation', 'PositionConclusion'):
        model = schema['$defs'][name]
        model['properties'].pop('highlight', None)
        for field in model['properties'].values():
            field.pop('default', None)
        model['required'] = list(model['properties'])
    return schema


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_NUMBER = re.compile(
    r"(?<!\w)(?<!\d[.,])[+−-]?"
    r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?(?!\d)(?![.,]\d)"
)
_GROUPED = re.compile(r"[+-]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:\.\d+)?")


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    value = value.strip().replace(",", ".").replace("−", "-")
    if _GROUPED.fullmatch(value):
        value = re.sub(r"[ \u00a0\u202f]", "", value)
    return Decimal(value) if len(value) <= 40 and _DECIMAL.fullmatch(value) else None


def _arithmetic(expression: str) -> Decimal:
    expression = expression.translate(str.maketrans({"×": "*", "÷": "/", "−": "-"}))
    tree = ast.parse(expression, mode="eval")

    def evaluate(node) -> Decimal:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            literal = ast.get_source_segment(expression, node) or ""
            value = _decimal(literal)
            if value is not None:
                return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise ValueError("Unsupported arithmetic expression")

    with localcontext() as context:
        context.prec = 50
        return evaluate(tree.body)


def _verify_calculation(direction: DirectionConclusion) -> bool:
    expected = _decimal(direction.source_value)
    actual = _decimal(direction.vor_value)
    deviation = _decimal(direction.deviation)
    calculation = direction.calculation
    try:
        with localcontext() as context:
            context.prec = 50
            if calculation is not None:
                reported = _decimal(calculation.result)
                if reported is None:
                    return False
                computed = _arithmetic(calculation.expression)
                precision = Decimal(1).scaleb(int(reported.as_tuple().exponent))
                if computed.quantize(precision, rounding=ROUND_HALF_UP) != reported:
                    return False
                if expected is not None and reported != expected:
                    return False
            if actual is not None and expected is not None and direction.deviation is not None:
                return deviation is not None and expected - actual == deviation
            return True
    except (SyntaxError, ValueError, DecimalException, OverflowError, RecursionError):
        return False


def _quantity_cited(direction: DirectionConclusion, required_kind: str | None) -> bool:
    value = _decimal(direction.source_value)
    if value is None or direction.calculation is not None:
        return True
    return any(
        value in _citation_numbers(source)
        for source in direction.sources
        if required_kind is None or source.kind == required_kind
    )


def _citation_numbers(source: Citation):
    quote = source.quote
    # An addressed numeric cell is independently verified in the pinned PDF.
    # Accept the OCR spacing after its decimal separator only for a whole cell.
    if source.kind in {'project', 'normative'} and source.evidence_id:
        compact = re.sub(r'(?<=[,.])\s+(?=\d)', '', quote.strip())
        if (value := _decimal(compact)) is not None:
            return [value]
    return [_decimal(match.group()) for match in _NUMBER.finditer(quote)]


def _operands_cited(direction: DirectionConclusion) -> bool:
    calculation = direction.calculation
    if calculation is None or calculation.operand_sources is None:
        return calculation is None
    expression = calculation.expression.translate(str.maketrans({'×': '*', '÷': '/', '−': '-'}))
    tree = ast.parse(expression, mode='eval')
    signed = {node.operand: node for node in ast.walk(tree)
              if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)}
    literals = sorted((signed.get(node, node) for node in ast.walk(tree) if isinstance(node, ast.Constant)),
                      key=lambda node: (node.lineno, node.col_offset))
    refs = calculation.operand_sources
    if len(literals) != len(refs):
        return False
    for node, ref in zip(literals, refs, strict=True):
        if not 0 <= ref < len(direction.sources):
            return False
        literal = _arithmetic(ast.get_source_segment(expression, node) or '')
        source = direction.sources[ref]
        if literal not in _citation_numbers(source):
            return False
    return True


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AuditContractError("Conclusion contains a duplicate JSON key")
    return value


def _unverified_direction(parameter, conclusion='Вывод требует перепроверки: сервер не смог проверить источник или расчёт.'):
    return DirectionConclusion(status='not_checked', parameter=parameter,
        vor_value=None, source_value=None, unit=None, calculation=None, deviation=None,
        conclusion=conclusion, sources=[], missing_facts=None)


def validate_result(
    raw: str | bytes,
    *,
    project_id: str,
    vor_version_id: str,
    expected_rows: Mapping[int, tuple[str, int, str]],
    verify_citation: Callable[[Citation], bool],
    vor_document_id: str,
    keep_independent: bool = False,
    on_rejected: Callable[[int, str, list[str]], None] | None = None,
    on_invalid_citation: Callable[[int, str, int], None] | None = None,
    on_missing_position: Callable[[int], None] | None = None,
    expected_vor_values: Mapping[int, tuple[str, str]] | None = None,
) -> AuditConclusion:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        schema_rejections = []
        if keep_independent and isinstance(data, dict) and isinstance(data.get('positions'), list):
            for position in data['positions']:
                if not isinstance(position, dict):
                    continue
                for name in DIRECTIONS:
                    try:
                        DirectionConclusion.model_validate(position.get(name))
                    except ValidationError:
                        position[name] = _unverified_direction('Проверка направления').model_dump()
                        schema_rejections.append((position.get('position_index'), name))
                findings = position.get('parameter_findings', [])
                if isinstance(findings, list):
                    for ordinal, item in enumerate(findings[:MAX_ADDITIONAL_FINDINGS]):
                        if not isinstance(item, dict) or item.get('direction') not in DIRECTIONS:
                            findings[ordinal] = {'direction': 'quantities', 'finding':
                                _unverified_direction('Проверка параметра').model_dump()}
                            schema_rejections.append((position.get('position_index'), f'parameter:{ordinal + 1}'))
                            continue
                        try:
                            ParameterFinding.model_validate(item)
                        except ValidationError:
                            item['finding'] = _unverified_direction('Проверка параметра').model_dump()
                            schema_rejections.append((position.get('position_index'), f"{item['direction']}:{ordinal + 1}"))
        result = AuditConclusion.model_validate(data)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise AuditContractError("Conclusion is not valid JSON") from None
    except ValidationError as exc:
        raise AuditContractError(f"Invalid conclusion JSON ({exc.error_count()} schema violations)") from None

    if result.project_id != project_id or result.vor_version_id != vor_version_id:
        raise AuditContractError("Conclusion refers to another project or VOR version")
    rows = {position.position_index: position for position in result.positions}
    if len(rows) != len(result.positions) or not rows.keys() <= expected_rows.keys():
        raise AuditContractError("Conclusion must cover every expected VOR position exactly once")
    missing = expected_rows.keys() - rows.keys()
    if missing and not keep_independent:
        raise AuditContractError("Conclusion must cover every expected VOR position exactly once")

    if on_rejected:
        for index, name in schema_rejections:
            on_rejected(index, name, ['schema'])

    verified: dict[tuple, tuple[bool, list[float] | None]] = {}
    invalid_citations = 0
    unsupported = []
    for index, position in rows.items():
        sheet, row, work_name = expected_rows[index]
        if (position.sheet, position.row, normalize_text(position.work_name)) != (
            sheet, row, normalize_text(work_name),
        ):
            raise AuditContractError(f"Workbook identity mismatch for position index {index}")
        main_findings = [(name, getattr(position, name), None) for name in DIRECTIONS]
        parameter_findings = [(item.direction, item.finding, ordinal)
                              for ordinal, item in enumerate(position.parameter_findings)]
        for name, direction, ordinal in [*main_findings, *parameter_findings]:
            label = name if ordinal is None else f'{name}:{ordinal + 1}'
            problems = []
            bad_sources = []
            for source_index, source in enumerate(direction.sources):
                source.highlight = None
                key = (source.document_id, source.kind, source.page, source.sheet, source.row, source.quote, source.evidence_id)
                if key not in verified:
                    ok = (
                        (source.kind != "vor" or source.document_id == vor_document_id)
                        and verify_citation(source)
                    )
                    verified[key] = ok, source.highlight if ok else None
                ok, source.highlight = verified[key]
                if not ok:
                    invalid_citations += 1
                    bad_sources.append(source_index)
                    if on_invalid_citation:
                        on_invalid_citation(index, label, source_index)
            if bad_sources:
                problems.append('citation')
                if keep_independent and direction.status not in DETERMINATE and direction.calculation:
                    operands = direction.calculation.operand_sources or []
                    if all(index not in bad_sources for index in operands):
                        retained = [(index, source) for index, source in enumerate(direction.sources)
                                    if index not in bad_sources]
                        direction.sources = [source for _, source in retained]
                        mapped = {old: new for new, (old, _) in enumerate(retained)}
                        direction.calculation.operand_sources = [mapped.get(index, -1) for index in operands]
                        direction.status = 'not_checked'
                        direction.missing_facts = None
                        direction.conclusion = 'Расчёт сохранён. Часть исходных ссылок требует повторной проверки.'
                        problems.remove('citation')
                        if on_rejected:
                            on_rejected(index, label, ['partial_citation'])
            required_kind = None if name == "quantities" else name
            if direction.status in DETERMINATE and (
                (required_kind is not None and not any(s.kind == required_kind for s in direction.sources))
                or not _quantity_cited(direction, required_kind)
            ):
                unsupported.append(f"{index}:{name}:evidence")
                problems.append('evidence')
            if (keep_independent and direction.status in DETERMINATE and required_kind is not None
                    and direction.calculation is not None):
                references = direction.calculation.operand_sources or []
                if not any(0 <= ref < len(direction.sources) and direction.sources[ref].kind == required_kind
                           for ref in references):
                    problems.append(f'{required_kind}_operand')
            if expected_vor_values is not None and index in expected_vor_values:
                expected_value, expected_unit = expected_vor_values[index]
                if not _matches_vor_value(direction, expected_value, expected_unit):
                    unsupported.append(f"{index}:{name}:vor_value")
                    problems.append('vor_value')
            if not _verify_calculation(direction) or (keep_independent and not _operands_cited(direction)):
                unsupported.append(f"{index}:{name}:calculation")
                problems.append('calculation')
            if keep_independent and problems:
                if on_rejected:
                    on_rejected(index, label, sorted(set(problems)))
                replacement = _unverified_direction(direction.parameter)
                if ordinal is None:
                    setattr(position, name, replacement)
                else:
                    position.parameter_findings[ordinal].finding = replacement

    if not keep_independent and (invalid_citations or unsupported):
        raise AuditContractError(
            f"Technical validation failed: {invalid_citations} invalid citations, "
            f"{len(unsupported)} unsupported checks ({', '.join(unsupported[:10])})",
            invalid_citations=invalid_citations,
        )
    for index in sorted(missing):
        sheet, row, work_name = expected_rows[index]
        result.positions.append(PositionConclusion(position_index=index, sheet=sheet, row=row,
            work_name=work_name, group='Не определена', **{
                name: _unverified_direction('Проверка направления', 'Codex не вернул заключение для этой позиции.')
                for name in DIRECTIONS
            }))
        if on_missing_position:
            on_missing_position(index)
    result.positions.sort(key=lambda position: position.position_index)
    return result


def _matches_vor_value(direction: DirectionConclusion, expected_value: str, expected_unit: str) -> bool:
    from audit_measurements import canonical_unit, normalize_unit_text

    reported = _decimal(direction.vor_value)
    expected = _decimal(expected_value)
    if reported is None or expected is None or not direction.unit:
        return True
    expected_key = canonical_unit(expected_unit)
    reported_key = canonical_unit(direction.unit)
    if not (reported_key == expected_key if reported_key and expected_key
            else normalize_unit_text(direction.unit) == normalize_unit_text(expected_unit)):
        return True  # This direction may check a different parameter, e.g. layer thickness.
    with localcontext() as context:
        context.prec = 50
        precision = Decimal(1).scaleb(int(reported.as_tuple().exponent))
        return reported == expected.quantize(precision, rounding=ROUND_HALF_UP)


def aggregate_status(statuses) -> Status:
    statuses = set(statuses)
    for status in ("mismatch", "needs_facts", "not_checked", "confirmed"):
        if status in statuses:
            return status
    return "not_applicable"


def position_status(position: PositionConclusion) -> Status:
    return aggregate_status(finding.status for _, finding in position.findings())


def result_metrics(result: AuditConclusion) -> dict:
    overall = Counter({status: 0 for status in STATES})
    directions = {name: Counter({status: 0 for status in STATES}) for name in DIRECTIONS}
    calculations = 0
    checked_calculations = 0
    confirmed_project = 0
    checked_normative = 0
    confirmed_normative = 0
    citations = 0
    completed = 0
    for position in result.positions:
        status = position_status(position)
        overall[status] += 1
        if all(finding.status not in {'needs_facts', 'not_checked'} for _, finding in position.findings()):
            completed += 1
        for name in DIRECTIONS:
            directions[name][aggregate_status(finding.status for key, finding in position.findings() if key == name)] += 1
        for name, direction in position.findings():
            citations += len(direction.sources)
            confirmed_project += name == 'project' and direction.status == 'confirmed'
            checked_normative += name == 'normative' and direction.status in DETERMINATE
            confirmed_normative += name == 'normative' and direction.status == 'confirmed'
            if direction.calculation is not None:
                calculations += 1
                checked_calculations += direction.status == "confirmed"
    total = len(result.positions)
    return {
        "total_positions": total,
        "completed_conclusions": completed,
        "overall_statuses": dict(overall),
        "direction_statuses": {name: dict(counts) for name, counts in directions.items()},
        "completed_share": completed / total,
        "mismatch_share": overall["mismatch"] / total,
        "needs_facts_share": overall["needs_facts"] / total,
        "not_checked_share": overall["not_checked"] / total,
        "confirmed_project_parameters": confirmed_project,
        "confirmed_calculations": checked_calculations,
        "reproducible_calculations": calculations,
        "confirmed_normative_conditions": confirmed_normative,
        "checked_normative_conditions": checked_normative,
        "verified_citations": citations,
    }
