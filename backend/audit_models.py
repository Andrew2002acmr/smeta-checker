"""Typed domain models: VOR input, LLM evidence, audit results, settings.

Pandas DataFrames and implicit dicts remain only at import/export boundaries
(Excel/CSV parsing, API responses, Word reports). Between modules
the audit exchanges these Pydantic models; legacy dict helpers preserve the
historical ``run_audit``/``tracking`` shapes for the UI, baseline and scripts.
"""

from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any, NamedTuple
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class LlmCredentials(NamedTuple):
    """Resolved provider: key/folder/model plus the transport/backend to use."""

    api_key: str
    folder: str
    model_name: str
    base_url: str
    provider: str


class AuditStatus(StrEnum):
    """Machine-checked audit outcome; ``ОК`` requires all confirmations."""

    OK = "ОК"
    ERROR = "Ошибка"
    INCOMPLETE = "Неполная проверка"
    TECHNICAL_ERROR = "Технический сбой"


class ReportStatus(StrEnum):
    """User-facing report bucket; derived from flags, never invented."""

    DISCREPANCY = "Обнаружено расхождение"
    MANUAL = "Требуется ручная проверка"
    NOT_CHECKED = "Проверка не выполнена"
    OK = "ОК"


class EvidenceKind(StrEnum):
    """Which claim one verbatim quote proves."""

    WORK = "work"
    VOLUME = "volume"
    UNIT = "unit"
    COMMENT = "comment"
    TECHNOLOGY = "technology"


class ClaimState(StrEnum):
    """Per-requirement outcome; unknown stays explicit, never silent."""

    CONFIRMED = "confirmed"
    CONTRADICTION = "contradiction"
    NOT_FOUND = "not_found"
    SOURCE_CONFLICT = "source_conflict"
    REVIEW_REQUIRED = "review_required"
    NOT_APPLICABLE = "not_applicable"


class ClaimKind(StrEnum):
    """Requirement family derived from VOR cells, never from PDF text."""

    PRESENCE = "presence"
    QUANTITY = "quantity"
    UNIT = "unit"
    GEOMETRY = "geometry"
    METHOD = "method"
    ATTRIBUTE = "attribute"
    BASIS = "basis"
    WORKBOOK = "workbook"


class ClaimSpec(BaseModel):
    """One checkable VOR requirement with its source cells."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    kind: ClaimKind
    label: str = ""
    cells: list[str] = Field(default_factory=list)
    expected_text: str = ""
    expected_value: str = ""
    expected_unit: str = ""
    required: bool = True
    predicate: str = ""
    property_label: str = ""


class EvidenceFact(BaseModel):
    """One code-validated fact bound to prepared source IDs."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    value: str = ""
    unit: str = ""
    source_ids: list[str] = Field(default_factory=list)
    value_source_id: str = ""
    unit_source_id: str = ""
    subject_source_ids: list[str] = Field(default_factory=list)
    relation_source_ids: list[str] = Field(default_factory=list)
    locators: list[dict[str, Any]] = Field(default_factory=list)


class ClaimCheck(BaseModel):
    """Evaluated requirement with machine-readable reason codes."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    kind: ClaimKind
    state: ClaimState
    reasons: list[str] = Field(default_factory=list)
    facts: list[EvidenceFact] = Field(default_factory=list)
    required: bool = True


def claim_status(checks, execution_errors=(), *, pdf_evaluated=True):
    """One precedence rule for all new evaluator/worker/report consumers."""
    def value(check, name, default=None):
        return check.get(name, default) if isinstance(check, dict) else getattr(check, name, default)
    required = [check for check in checks if value(check, "required", True)]
    if any(value(check, "state") == ClaimState.CONTRADICTION for check in required):
        return AuditStatus.ERROR.value
    if execution_errors and not pdf_evaluated:
        return AuditStatus.TECHNICAL_ERROR.value
    if execution_errors or not required or any(
            value(check, "state") not in {ClaimState.CONFIRMED, ClaimState.NOT_APPLICABLE}
            for check in required):
        return AuditStatus.INCOMPLETE.value
    if not pdf_evaluated:
        # Only workbook Excel checks confirmed while PDF was never evaluated
        # (truncated answer, exhausted budget, bind failure): not an audit OK.
        return AuditStatus.INCOMPLETE.value
    return AuditStatus.OK.value


class ErrorKind(StrEnum):
    """Explicit failure taxonomy; messages stay human-readable."""

    BIND = "bind"
    PREP = "prep"
    VALIDATION = "validation"
    PROVIDER = "provider"
    BUDGET = "budget"
    INTERNAL = "internal"


class AuditError(BaseModel):
    """Structured failure; legacy string errors map to ``message``."""

    model_config = ConfigDict(extra="forbid")

    kind: ErrorKind
    message: str = Field(min_length=1)
    recoverable: bool = False

    @field_validator("message", mode="before")
    @classmethod
    def _strip_message(cls, value: Any) -> Any:
        return str(value).strip() if value is not None else value


class Document(BaseModel):
    """One PDF in the pool; ``id`` is the evidence namespace."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = ""
    page_count: int | None = Field(default=None, ge=0)

    @field_validator("id", "name", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return str(value).strip() if value is not None else value


class VorMeta(BaseModel):
    """VOR workbook header; defaults match the historical parser."""

    model_config = ConfigDict(extra="forbid")

    object_name: str = "Не указано"
    doc_number: str = ""
    basis_code: str = "Проектная документация"

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class SourceHeading(BaseModel):
    """Original section boundary, independent of quantities."""

    model_config = ConfigDict(extra="forbid")
    address: str
    text: str


class PositionSource(BaseModel):
    """Identity and coordinates of an unfiltered source record."""

    model_config = ConfigDict(extra="forbid")
    document_sha256: str
    format: str
    sheet: str = ""
    row: int = Field(ge=1)
    header_row: int = Field(ge=1)
    position_id: str
    cells: dict[str, str] = Field(default_factory=dict)
    continuation_rows: list[int] = Field(default_factory=list)
    sections: list[SourceHeading] = Field(default_factory=list)
    scope_key: str = ""
    scope_reason: str = "unknown"


def position_source_dict(position: Any) -> dict[str, Any] | None:
    """Serialize provenance from typed rows or prepared plain-data siblings."""
    source = getattr(position, "position_source", None)
    if isinstance(source, PositionSource):
        return source.model_dump(mode="json")
    return copy.deepcopy(source) if isinstance(source, dict) else None


def expected_scope_hint(source: Any) -> str | None:
    """Legacy None means provenance omitted; "" is explicit unknown scope."""
    if source is None:
        return None
    if isinstance(source, PositionSource):
        return source.scope_key
    if isinstance(source, dict):
        return str(source.get("scope_key", ""))
    return None


def serialize_position_result(
    task: dict[str, Any],
    res_item: dict[str, Any],
    track_item: dict[str, Any],
    pdf_versions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single stored payload shape for direct, Celery and finalize paths."""
    payload = {
        "status": res_item.get("Статус аудита"),
        "work_name": track_item.get("work_name", task.get("w_name")),
        "work_found": bool(res_item.get("work_found")),
        "volume_confirmed": bool(res_item.get("volume_confirmed")),
        "unit_confirmed": bool(res_item.get("unit_confirmed")),
        "comment_confirmed": bool(res_item.get("comment_confirmed")),
        "found_volume": res_item.get("Фактический объем", "-"),
        "found_unit": track_item.get("found_unit", "-"),
        "found_page": track_item.get("found_page"),
        "found_pages": list(track_item.get("found_pages") or []),
        "vor_pages": list(track_item.get("vor_pages") or []),
        "evidence": [dict(item) for item in track_item.get("evidence") or []],
        "volume_calculation": track_item.get("volume_calculation"),
        "technology": track_item.get("technology", {}),
        "quantity_basis": track_item.get("quantity_basis", "unknown"),
        "quantity_kind": track_item.get("quantity_kind", "direct"),
        "consumption": track_item.get("consumption"),
        "workbook_calculation": track_item.get("workbook_calculation", {}),
        "workbook_notes": list(track_item.get("workbook_notes") or []),
        "checks": [dict(check) for check in track_item.get("checks", [])],
        "execution_errors": [dict(item) for item in track_item.get("execution_errors", [])],
        "evaluation_version": track_item.get("evaluation_version", ""),
        "corpus_id": track_item.get("corpus_id", task.get("corpus_id", "")),
        "position_source": track_item.get("position_source")
        or task.get("position_source"),
        "position_type": track_item.get("position_type", task.get("position_type", "")),
        "discrepancy_details": res_item.get("Детали расхождений", ""),
        "search_complete": bool(track_item.get("search_complete")),
        "requests": track_item.get("requests", 0),
        "contexts": track_item.get("contexts", 0),
        "budget_tokens": res_item.get("budget_tokens", track_item.get("budget_tokens")),
        "usage": dict(track_item.get("usage") or res_item.get("usage") or {}),
        "estimated_cost": track_item.get("estimated_cost"),
        "pdf_name": track_item.get("pdf_name", ""),
        "trace_id": track_item.get("trace_id"),
        "trace_url": track_item.get("trace_url"),
    }
    versions = pdf_versions or {}
    pdf_name = track_item.get("pdf_name") or ""
    if pdf_name in versions:
        row = versions[pdf_name]
        payload["pdf_version_id"] = str(row.id)
        payload["pdf_sha256"] = row.sha256
    return payload


class SourceLocator(BaseModel):
    """Prepared evidence coordinates, never supplied by the model."""

    model_config = ConfigDict(extra="forbid")
    source_id: str
    document_id: str = ""
    page: int = Field(ge=1)
    text: str = ""
    bbox: list[float] = Field(default_factory=list)
    origin: str = "native"
    table_id: str = ""
    row_id: str = ""
    column_id: str = ""
    header_ids: list[str] = Field(default_factory=list)
    group_id: str = ""
    scope_key: str = ""
    role: str = ""
    quality: str = "usable"
    confidence: float | None = None
    rotation: int = 0
    crop: list[float] = Field(default_factory=list)
    transform: list[float] = Field(default_factory=list)


class VorPosition(BaseModel):
    """One validated VOR row plus its derived binding references.

    Runtime handles (``PageTextIndex``, ``PositionTrace``) are intentionally
    excluded: they are thread-bound and never serialized.
    """

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    number: str = ""
    work_name: str = Field(min_length=1)
    volume: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    reference: str = ""
    page_ref: str = ""
    file_ref: str = ""
    explicit_refs: list[str] = Field(default_factory=list)
    designation_refs: list[str] = Field(default_factory=list)
    comment: str = ""
    formula: str = ""
    formula_refs: dict[str, Any] = Field(default_factory=dict)
    position_type: str = ""
    calculation: dict[str, Any] = Field(default_factory=dict)
    position_source: PositionSource | None = None
    document_id: str = ""
    bind_error: str | None = None
    prep_error: str | None = None
    bind_error_kind: ErrorKind | None = None
    prep_error_kind: ErrorKind | None = None
    target_indices: list[int] = Field(default_factory=list)
    location_description: str = "Страница не указана"
    primary_pages: list[int] = Field(default_factory=list)

    @field_validator("target_indices", mode="before")
    @classmethod
    def _validate_targets(cls, value: Any) -> Any:
        items = list(value or [])
        for item in items:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("target_indices must hold 0-based page indices")
        return items

    @field_validator("primary_pages", mode="before")
    @classmethod
    def _validate_primary(cls, value: Any) -> Any:
        items = list(value or [])
        for item in items:
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError("primary_pages must hold 1-based page numbers")
        return items

    @field_validator("explicit_refs", "designation_refs", mode="before")
    @classmethod
    def _validate_refs(cls, value: Any) -> Any:
        return [str(item) for item in (value or [])]

    def legacy_task(self) -> dict[str, Any]:
        """Task-dict view without runtime handles (trace/index added by caller)."""
        return {
            "idx": self.index,
            "number": self.number,
            "w_name": self.work_name,
            "w_vol": self.volume,
            "w_unit": self.unit,
            "w_ref": self.reference,
            "w_page": self.page_ref,
            "w_comment": self.comment,
            "w_formula": self.formula,
            "position_type": self.position_type,
            "calculation": copy.deepcopy(self.calculation),
            "formula_refs": copy.deepcopy(self.formula_refs),
            "position_source": self.position_source.model_dump(mode="json") if self.position_source else None,
            "target_pdf_name": self.document_id,
            "explicit_refs": list(self.explicit_refs),
            "designation_refs": list(self.designation_refs),
            "bind_error": self.bind_error,
            "prep_error": self.prep_error,
            "target_indices": list(self.target_indices),
            "loc_desc": self.location_description,
            "primary_phys_pages": list(self.primary_pages),
            "file_refs": self.file_ref,
        }

    @classmethod
    def from_legacy_task(cls, task: dict[str, Any]) -> "VorPosition":
        """Validate a historical task/report dict; runtime handles are ignored."""
        return cls(
            index=int(task.get("idx", 0)),
            number=str(task.get("number", task.get("№", ""))),
            work_name=str(task.get("w_name", "")),
            volume=str(task.get("w_vol", "")),
            unit=str(task.get("w_unit", "")),
            reference=str(task.get("w_ref", "") or ""),
            page_ref=str(task.get("w_page", "") or ""),
            file_ref=str(task.get("file_refs", task.get("file_ref", "")) or ""),
            explicit_refs=list(task.get("explicit_refs", []) or []),
            designation_refs=list(task.get("designation_refs", []) or []),
            comment=str(task.get("w_comment", "") or ""),
            formula=str(task.get("w_formula", "") or ""),
            position_type=str(task.get("position_type", "") or ""),
            calculation=copy.deepcopy(task.get("calculation", {}) or {}),
            formula_refs=copy.deepcopy(task.get("formula_refs", {}) or {}),
            position_source=task.get("position_source"),
            document_id=str(task.get("target_pdf_name", "") or ""),
            bind_error=task.get("bind_error"),
            prep_error=task.get("prep_error"),
            target_indices=list(task.get("target_indices", []) or []),
            location_description=str(
                task.get("loc_desc", "Страница не указана") or "Страница не указана"
            ),
            primary_pages=list(task.get("primary_phys_pages", []) or []),
        )


class Evidence(BaseModel):
    """One verbatim quote; ``document_id`` namespaces the physical page."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = ""
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=1200)
    kind: EvidenceKind

    @field_validator("page", mode="before")
    @classmethod
    def _validate_page(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("page must be a 1-based integer")
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("quote", mode="before")
    @classmethod
    def _strip_quote(cls, value: Any) -> Any:
        return str(value).strip() if isinstance(value, str) else value

    @field_validator("document_id", mode="before")
    @classmethod
    def _strip_document(cls, value: Any) -> Any:
        return str(value or "").strip()

    @classmethod
    def from_legacy(cls, item: dict[str, Any], document_id: str = "") -> "Evidence":
        """Accept historical ``{page, quote, kind}`` entries."""
        page: Any = item.get("page")
        kind: Any = item.get("kind")
        return cls(
            document_id=str(item.get("document_id", document_id) or ""),
            page=page,
            quote=item.get("quote", ""),
            kind=kind,
        )


class VolumeOperand(BaseModel):
    """One S×t factor; page/quote optional for display-only legacy rows."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=32)
    unit: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=1200)
    source_id: str | None = None
    document_id: str = ""

    @field_validator("page", mode="before")
    @classmethod
    def _validate_page(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("operand page must be a 1-based integer")
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("value", "unit", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return str(value).strip() if value is not None else value

    @field_validator("quote", mode="before")
    @classmethod
    def _strip_quote(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class VolumeCalculation(BaseModel):
    """Area × thickness derived only from dimensioned PDF evidence."""

    model_config = ConfigDict(extra="forbid")

    area: VolumeOperand
    thickness: VolumeOperand


class TechnologyStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    NOT_APPLICABLE = "not_applicable"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    DIFFERENT = "different"


class TechnologyAssessment(BaseModel):
    """Technology is independent of operation presence and quantity."""

    model_config = ConfigDict(extra="forbid")
    status: TechnologyStatus = TechnologyStatus.NOT_CHECKED
    expected: str = ""
    actual: str = ""
    cross_pages: list[int] = Field(default_factory=list)


class ConsumptionAssessment(BaseModel):
    """V = S × q reconciliation: workbook rate vs schedule-derived q_pd."""

    model_config = ConfigDict(extra="forbid")

    rate: str = ""
    unit: str = ""
    area: str = ""
    schedule_volume: str = ""
    schedule_page: int | None = None
    schedule_item: str = ""
    derived_rate: str = ""
    delta: str = ""
    delta_percent: str = ""
    matches: bool | None = None
    uncertainties: list[str] = Field(default_factory=list)


class AuditResult(BaseModel):
    """Validated per-position outcome; ``status`` never overrides evidence."""

    model_config = ConfigDict(extra="ignore")

    status: AuditStatus
    work_found: StrictBool
    volume_confirmed: StrictBool
    unit_confirmed: StrictBool
    comment_confirmed: StrictBool
    found_volume: str = "-"
    found_unit: str = "-"
    found_page: str = "-"
    found_pages: list[int] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    discrepancy_details: str = ""
    search_complete: StrictBool = False
    requests: int = Field(default=0, ge=0)
    contexts: int = Field(default=0, ge=0)
    budget_tokens: int | None = Field(default=None, ge=0)
    usage: dict[str, int] = Field(default_factory=dict)
    volume_calculation: VolumeCalculation | None = None
    technology: TechnologyAssessment = Field(default_factory=TechnologyAssessment)
    quantity_basis: str = "unknown"
    consumption: ConsumptionAssessment | None = None
    stock_chain: dict[str, Any] | None = None
    workbook_calculation: dict[str, Any] = Field(default_factory=dict)
    workbook_notes: list[dict[str, Any]] = Field(default_factory=list)
    position_source: PositionSource | None = None
    checks: list[ClaimCheck] = Field(default_factory=list)
    execution_errors: list[AuditError] = Field(default_factory=list)
    evaluation_version: str = ""
    corpus_id: str = ""
    recoverable: bool | None = None
    error: AuditError | None = None

    @field_validator("usage", mode="before")
    @classmethod
    def _validate_usage(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("usage must be a mapping")
        cleaned: dict[str, int] = {}
        for key in ("input", "output", "total", "reasoning"):
            item = value.get(key)
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("usage tokens must be non-negative integers")
            cleaned[key] = item
        return cleaned

    @field_validator("found_volume", "found_unit", "found_page", mode="before")
    @classmethod
    def _stringify_found(cls, value: Any) -> Any:
        if value is None:
            return "-"
        if isinstance(value, bool):
            raise ValueError("found_* must not be boolean")
        if isinstance(value, (int, float)):
            return str(value).strip() or "-"
        text = str(value).strip()
        return text or "-"

    @field_validator("found_pages", mode="before")
    @classmethod
    def _validate_pages(cls, value: Any) -> Any:
        items = []
        for item in value or []:
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError("found_pages must hold 1-based page numbers")
            items.append(item)
        return sorted(set(items))

    @field_validator("discrepancy_details", mode="before")
    @classmethod
    def _stringify_details(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            raise ValueError("discrepancy_details must be text")
        return str(value)

    @field_validator("requests", "contexts", mode="before")
    @classmethod
    def _validate_counters(cls, value: Any) -> Any:
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("counters must be non-negative integers")
        return value

    def model_post_init(self, _context: Any) -> None:
        if self.status == AuditStatus.OK and self.evaluation_version == "evidence-claims-v1":
            required = [check for check in self.checks if check.required]
            identity = {check.claim_id: check.state for check in required}
            if any(identity.get(name) != ClaimState.CONFIRMED
                   for name in ("presence", "quantity", "unit")):
                raise ValueError("ОК requires independently bound presence/quantity/unit")
            if not required or any(
                    check.state not in {ClaimState.CONFIRMED, ClaimState.NOT_APPLICABLE}
                    for check in required):
                raise ValueError("ОК requires every required claim check")
            if self.execution_errors:
                raise ValueError("ОК requires no unresolved execution errors")
            return
        if self.status == AuditStatus.OK and not (
            self.work_found
            and self.volume_confirmed
            and self.unit_confirmed
        ):
            raise ValueError("ОК requires all confirmations")
        if self.status == AuditStatus.OK and self.technology.status in {
            TechnologyStatus.DIFFERENT, TechnologyStatus.UNCONFIRMED,
        }:
            raise ValueError("ОК requires a resolved technology assessment")

    def to_legacy(self) -> dict[str, Any]:
        """Historical dict shape; evidence keeps ``document_id`` for provenance."""
        data = self.model_dump(mode="json", exclude_none=True)
        # Legacy readers expect plain dicts; drop the structured error wrapper
        # while keeping its human-readable details in ``discrepancy_details``.
        data.pop("error", None)
        if self.recoverable is None:
            data.pop("recoverable", None)
        for item in data.get("evidence") or []:
            if isinstance(item, dict) and not item.get("document_id"):
                item.pop("document_id", None)
        calculation = data.get("volume_calculation")
        if isinstance(calculation, dict):
            for operand in calculation.values():
                if isinstance(operand, dict):
                    if not operand.get("document_id"):
                        operand.pop("document_id", None)
                    if not operand.get("source_id"):
                        operand.pop("source_id", None)
        return data

    @classmethod
    def from_legacy(cls, data: dict[str, Any]) -> "AuditResult":
        """Validate a historical result/tracking dict."""
        payload = dict(data)
        payload["evidence"] = [
            item
            if isinstance(item, Evidence)
            else Evidence.from_legacy(item)
            if isinstance(item, dict)
            else item
            for item in (data.get("evidence") or [])
        ]
        return cls(**payload)


class LlmClaimProposal(BaseModel):
    """One model-proposed fact: claim ID plus prepared source IDs only."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    value_source_id: str = ""
    unit_source_id: str = ""
    subject_source_ids: list[str] = Field(default_factory=list)
    relation_source_ids: list[str] = Field(default_factory=list)

    @field_validator("source_ids", "subject_source_ids", "relation_source_ids", mode="before")
    @classmethod
    def _require_id_lists(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("source IDs must be a list of non-empty strings")
        return [item.strip() for item in value]

    @field_validator("value_source_id", "unit_source_id", mode="before")
    @classmethod
    def _strip_optional_id(cls, value: Any) -> Any:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("optional source ID must be a string")
        return value.strip()


class LlmAuditResponse(BaseModel):
    """Wire shape of one LLM answer: claim proposals, never verdicts."""

    model_config = ConfigDict(extra="forbid")

    # Individual records are validated by the evaluator, so a malformed
    # proposal cannot discard unrelated valid evidence in the same answer.
    claims: list[Any]
    discrepancy_details: str | int | float | None = ""

    @field_validator("discrepancy_details", mode="before")
    @classmethod
    def _stringify_details(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            raise ValueError("discrepancy_details must not be boolean")
        if not isinstance(value, (str, int, float)):
            raise ValueError("discrepancy_details has an unsupported type")
        return str(value).strip()


class AuditSettings(BaseModel):
    """Typed project settings; resolution stays ``.env`` → env → secrets."""

    model_config = ConfigDict(extra="forbid")

    yandex_cloud_folder: str = ""
    yandex_cloud_api_key: str = ""
    yandex_cloud_model: str = "qwen3.6-35b-a3b/latest"
    # Generic OpenAI-compatible backend (AnyModel gateway). Empty values keep
    # the historical Yandex path: base_url defaults to Yandex, the model keeps
    # the ``gpt://`` shape and the folder rides the ``project`` slot.
    llm_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"
    telemetry_redact_pdf: bool = False
    telemetry_mask_prompts: bool = False
    database_url: str = ""
    blob_store_url: str = ""
    broker_url: str = ""
    pdf_queue_size: int = 2
    llm_queue_size: int = 8
    task_time_limit: int = 600
    task_soft_time_limit: int = 540
    max_upload_bytes: int = 100 * 1024 * 1024
    max_uploads_per_request: int = 32
    api_token: str = ""
    allowed_llm_hosts: tuple[str, ...] = ("ai.api.cloud.yandex.net",)
    llm_input_cost_per_1k: float = 0.0
    llm_output_cost_per_1k: float = 0.0

    @classmethod
    def load(cls, file_values: Any = None, secrets: Any = None) -> "AuditSettings":
        """Resolve every key through the historical ``get_setting`` precedence."""
        from audit_settings import get_setting

        def _truthy(name: str) -> bool:
            value = get_setting(name, "", file_values=file_values, secrets=secrets)
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def _csv(name: str) -> tuple[str, ...]:
            raw = get_setting(name, "", file_values=file_values, secrets=secrets)
            return tuple(part.strip() for part in str(raw).split(",") if part.strip())

        llm_hosts = _csv("AUDIT_ALLOWED_LLM_HOSTS") or ("ai.api.cloud.yandex.net",)

        llm_api_key = get_setting(
            "AUDIT_LLM_API_KEY",
            get_setting(
                "ANYMODEL_API_KEY", "",
                file_values=file_values, secrets=secrets,
            ),
            file_values=file_values,
            secrets=secrets,
        )
        llm_model = get_setting(
            "AUDIT_LLM_MODEL",
            get_setting(
                "ANYMODEL_MODEL", "",
                file_values=file_values, secrets=secrets,
            ),
            file_values=file_values,
            secrets=secrets,
        )
        # AnyModel keys select the gateway default; a bare AUDIT_* override
        # without a chat host stays on Yandex (the adapter still routes by
        # host, so an explicit Yandex base keeps the Responses shape).
        anymodel_base = get_setting(
            "ANYMODEL_BASE_URL", "",
            file_values=file_values, secrets=secrets,
        )
        llm_base_url = get_setting(
            "AUDIT_LLM_BASE_URL",
            anymodel_base
            or (
                "https://anymodel.org/v1"
                if llm_api_key.strip() or llm_model.strip()
                else "https://ai.api.cloud.yandex.net/v1"
            ),
            file_values=file_values,
            secrets=secrets,
        )
        router_key = get_setting('ROUTERAI_API_KEY', '', file_values=file_values, secrets=secrets)
        router_model = get_setting('ROUTERAI_MODEL', '', file_values=file_values, secrets=secrets)
        if router_key or router_model:
            # Select one complete provider namespace; never send an AnyModel
            # key/model to RouterAI or silently fall back after partial setup.
            llm_api_key, llm_model = router_key, router_model
            llm_base_url = get_setting('ROUTERAI_BASE_URL', 'https://routerai.ru/api/v1',
                file_values=file_values, secrets=secrets)
        # Trust the host of the explicitly selected backend: AnyModel keys
        # route traffic there, so requiring a second manual allowlist entry
        # only produces a dead audit. Users who set neither key stay on the
        # Yandex default and keep the fail-closed check for anything else.
        if llm_api_key.strip() or llm_model.strip():
            try:
                selected_host = (
                    urlparse(str(llm_base_url)).hostname or ""
                ).lower()
            except ValueError:
                selected_host = ""
            if selected_host and selected_host not in {
                host.lower() for host in llm_hosts
            }:
                llm_hosts = (*llm_hosts, selected_host)
        return cls(
            yandex_cloud_folder=get_setting(
                "YANDEX_CLOUD_FOLDER", "", file_values=file_values, secrets=secrets
            ),
            yandex_cloud_api_key=get_setting(
                "YANDEX_CLOUD_API_KEY", "", file_values=file_values, secrets=secrets
            ),
            yandex_cloud_model=get_setting(
                "YANDEX_CLOUD_MODEL",
                "qwen3.6-35b-a3b/latest",
                file_values=file_values,
                secrets=secrets,
            ),
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            langfuse_public_key=get_setting(
                "LANGFUSE_PUBLIC_KEY", "", file_values=file_values, secrets=secrets
            ),
            langfuse_secret_key=get_setting(
                "LANGFUSE_SECRET_KEY", "", file_values=file_values, secrets=secrets
            ),
            langfuse_base_url=get_setting(
                "LANGFUSE_BASE_URL",
                "https://cloud.langfuse.com",
                file_values=file_values,
                secrets=secrets,
            ),
            langfuse_environment=get_setting(
                "LANGFUSE_ENVIRONMENT",
                "development",
                file_values=file_values,
                secrets=secrets,
            ),
            telemetry_redact_pdf=_truthy("AUDIT_TELEMETRY_REDACT_PDF"),
            telemetry_mask_prompts=_truthy("AUDIT_TELEMETRY_MASK_PROMPTS"),
            database_url=get_setting(
                "DATABASE_URL", "", file_values=file_values, secrets=secrets
            ),
            blob_store_url=get_setting(
                "BLOB_STORE_URL", "", file_values=file_values, secrets=secrets
            ),
            broker_url=get_setting(
                "CELERY_BROKER_URL", "", file_values=file_values, secrets=secrets
            ),
            pdf_queue_size=int(
                get_setting(
                    "AUDIT_PDF_CONCURRENCY",
                    "2",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 2
            ),
            llm_queue_size=int(
                get_setting(
                    "AUDIT_LLM_CONCURRENCY",
                    "8",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 8
            ),
            task_time_limit=int(
                get_setting(
                    "AUDIT_TASK_TIME_LIMIT",
                    "600",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 600
            ),
            task_soft_time_limit=int(
                get_setting(
                    "AUDIT_TASK_SOFT_TIME_LIMIT",
                    "540",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 540
            ),
            max_upload_bytes=int(
                get_setting(
                    "AUDIT_MAX_UPLOAD_BYTES",
                    str(100 * 1024 * 1024),
                    file_values=file_values,
                    secrets=secrets,
                )
                or 100 * 1024 * 1024
            ),
            max_uploads_per_request=int(
                get_setting(
                    "AUDIT_MAX_UPLOADS_PER_REQUEST",
                    "32",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 32
            ),
            api_token=get_setting(
                "AUDIT_API_TOKEN", "", file_values=file_values, secrets=secrets
            ),
            allowed_llm_hosts=llm_hosts,
            llm_input_cost_per_1k=float(
                get_setting(
                    "AUDIT_LLM_INPUT_COST_PER_1K",
                    "0",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 0
            ),
            llm_output_cost_per_1k=float(
                get_setting(
                    "AUDIT_LLM_OUTPUT_COST_PER_1K",
                    "0",
                    file_values=file_values,
                    secrets=secrets,
                )
                or 0
            ),
        )

    def llm_credentials(self) -> LlmCredentials:
        """Pick Yandex or a chat gateway; explicit keys beat implicit defaults.

        RouterAI or AnyModel keys select the chat backend
        with the raw catalog id. Otherwise Yandex keeps the historical
        ``gpt://folder/model`` shape.
        """
        router = (urlparse(self.llm_base_url).hostname or '').lower() == 'routerai.ru'
        if router or (self.llm_api_key.strip() and self.llm_model.strip()):
            return LlmCredentials(
                api_key=self.llm_api_key.strip() if self.llm_model.strip() else '',
                folder="",
                model_name=self.llm_model.strip(),
                base_url=self.llm_base_url.strip()
                or "https://ai.api.cloud.yandex.net/v1",
                provider=self._chat_provider_label(self.llm_base_url),
            )
        return LlmCredentials(
            api_key=self.yandex_cloud_api_key,
            folder=self.yandex_cloud_folder,
            model_name=self.yandex_cloud_model,
            base_url="https://ai.api.cloud.yandex.net/v1",
            provider="Yandex Cloud (Qwen)",
        )

    @staticmethod
    def _chat_provider_label(base_url: str) -> str:
        """Human backend label; unknown chat hosts stay generic."""
        try:
            host = (urlparse(str(base_url)).hostname or "").lower()
        except ValueError:
            host = ""
        if host == "anymodel.org":
            return "AnyModel (chat)"
        if host == 'routerai.ru':
            return 'RouterAI (chat)'
        return f"{host or base_url} (chat)"
