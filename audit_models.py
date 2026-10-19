"""Typed domain models: VOR input, LLM evidence, audit results, settings.

Pandas DataFrames and implicit dicts remain only at import/export boundaries
(Excel/CSV parsing, Streamlit display, Excel/Word reports). Between modules
the audit exchanges these Pydantic models; legacy dict helpers preserve the
historical ``run_audit``/``tracking`` shapes for the UI, baseline and scripts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


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
    comment: str = ""
    formula: str = ""
    document_id: str = ""
    bind_error: str | None = None
    prep_error: str | None = None
    bind_error_kind: ErrorKind | None = None
    prep_error_kind: ErrorKind | None = None
    target_indices: list[int] = Field(default_factory=list)
    location_description: str = "Страница не указана"
    primary_pages: list[int] = Field(default_factory=list)
    sheet: str = ""
    source_row: int | None = Field(default=None, ge=1)
    source_cells: dict[str, str] = Field(default_factory=dict)

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

    @field_validator("explicit_refs", mode="before")
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
            "target_pdf_name": self.document_id,
            "explicit_refs": list(self.explicit_refs),
            "bind_error": self.bind_error,
            "prep_error": self.prep_error,
            "target_indices": list(self.target_indices),
            "loc_desc": self.location_description,
            "primary_phys_pages": list(self.primary_pages),
            "file_refs": self.file_ref,
            "sheet": self.sheet,
            "source_row": self.source_row,
            "source_cells": dict(self.source_cells),
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
            sheet=str(task.get("sheet", "")),
            source_row=task.get("source_row"),
            source_cells=dict(task.get("source_cells") or {}),
            explicit_refs=list(task.get("explicit_refs", []) or []),
            comment=str(task.get("w_comment", "") or ""),
            formula=str(task.get("w_formula", "") or ""),
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
        for key in ("input", "output", "total"):
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
        if self.status == AuditStatus.OK and not (
            self.work_found
            and self.volume_confirmed
            and self.unit_confirmed
            and self.comment_confirmed
        ):
            raise ValueError("ОК requires all confirmations")
        if not self.work_found:
            # Absence of proof is not absence of work; keep explicit unknowns.
            object.__setattr__(self, "found_page", "-")
            object.__setattr__(self, "found_volume", "-")
            object.__setattr__(self, "found_unit", "-")

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


class LlmEvidenceRef(BaseModel):
    """Model-selected quote pointer; resolved against the sent fragments."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    kind: EvidenceKind


class LlmVolumeOperandRef(BaseModel):
    """Model-proposed S×t operand; value/unit checked against its quote."""

    model_config = ConfigDict(extra="forbid")

    value: str | int | float = Field()
    unit: str = Field(min_length=1)
    source_id: str = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def _stringify_value(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("operand value must not be boolean")
        return str(value).strip() if value is not None else value


class LlmVolumeCalculationRef(BaseModel):
    """Raw S×t proposal before verbatim-evidence checks."""

    model_config = ConfigDict(extra="forbid")

    area: LlmVolumeOperandRef
    thickness: LlmVolumeOperandRef


class LlmAuditResponse(BaseModel):
    """Wire shape of one LLM answer; context checks stay in ``audit_core``."""

    model_config = ConfigDict(extra="ignore")

    work_found: StrictBool
    volume_confirmed: StrictBool
    unit_confirmed: StrictBool
    comment_confirmed: StrictBool
    found_volume: str | int | float | None
    found_unit: str | int | float | None
    found_page: str | int | None
    evidence: list[dict[str, Any]]
    discrepancy_details: str | int | float | None
    volume_calculation: dict[str, Any] | None = None

    @field_validator("found_volume", "found_unit", "discrepancy_details", mode="before")
    @classmethod
    def _stringify_text(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("text field must not be boolean")
        if not isinstance(value, (str, int, float)):
            raise ValueError("text field has an unsupported type")
        return str(value).strip()

    @field_validator("found_page", mode="before")
    @classmethod
    def _stringify_page(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("found_page must not be boolean")
        if not isinstance(value, (str, int, float)):
            raise ValueError("found_page has an unsupported type")
        return str(value).strip()

    @field_validator("evidence", mode="before")
    @classmethod
    def _require_evidence_list(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("evidence must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("evidence items must be objects")
        return value


class AuditSettings(BaseModel):
    """Typed project settings; resolution stays ``.env`` → env → secrets."""

    model_config = ConfigDict(extra="forbid")

    yandex_cloud_folder: str = ""
    yandex_cloud_api_key: str = ""
    yandex_cloud_model: str = "qwen3.6-35b-a3b/latest"
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
