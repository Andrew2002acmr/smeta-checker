"""Public contracts for the Nuxt audit workspace."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, StrictBool

from audit_assessment import PositionAssessment
from audit_models import AuditStatus, ReportStatus, VorPosition
from codex_contract import PositionConclusion
class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("created_at", "finished_at", "revoked_at", check_fields=False)
    @classmethod
    def _utc_datetimes(cls, value):
        # Contract datetimes are UTC (Z-suffixed); storage may carry any zone.
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc)
        return value


class NormativeFacts(DTO):
    system: Literal["", "power", "contact_network", "lighting", "automation"] = ""
    placement: Literal["", "laid_pipes_blocks_boxes", "trench", "tray", "open"] = ""
    voltage_kv: str = Field(default="", max_length=32)
    mass_kg_per_m: str = Field(default="", max_length=32)
    height_m: str = Field(default="", max_length=32)
    cross_section_mm2: str = Field(default="", max_length=32)
    unit: str = Field(default="", max_length=32)
    quantity: str = Field(default="", max_length=64)
    included_height_exceeded: StrictBool | None = None
    separate_connection_position: StrictBool | None = None
    project_date: str = Field(default="", max_length=10)

    @field_validator("voltage_kv", "mass_kg_per_m", "height_m", "cross_section_mm2", "quantity")
    @classmethod
    def _decimal(cls, value):
        from decimal import Decimal, InvalidOperation
        if not value.strip():
            return ""
        try:
            number = Decimal(value.replace(",", ".").replace(" ", ""))
            if not number.is_finite() or not 0 <= number <= Decimal("1e12"):
                raise ValueError("Ожидается конечное неотрицательное число")
            return format(number, "f")
        except InvalidOperation as exc:
            raise ValueError("Введите число, а не выражение") from exc

    @field_validator("project_date")
    @classmethod
    def _date(cls, value):
        from datetime import date
        if value:
            date.fromisoformat(value)
        return value


class NormativeSearchRequest(DTO):
    query: str = Field(min_length=2, max_length=2000)
    kind: Literal["GESN", "SP", "GOST", "PUE", "ALL"] = "GESN"
    mode: Literal["lexical", "context"] = "lexical"
    facts: NormativeFacts = Field(default_factory=NormativeFacts)


class NormativeSelectionRequest(DTO):
    search_id: str
    norm_ids: list[str] = Field(max_length=20)
    comment: str = Field(default="", max_length=2000)


class NormativePositionCheckRequest(DTO):
    question: str = Field(default="", max_length=2000)
    pages: list[int] = Field(default_factory=list, max_length=6)
    pdf_version_id: str | None = None

    @field_validator("pages")
    @classmethod
    def _positive_pages(cls, values):
        if any(v < 1 or v > 10000 for v in values):
            raise ValueError("Укажите физические страницы PDF, начиная с 1")
        return list(dict.fromkeys(values))


class NormativeCitation(DTO):
    source_id: str
    designation: str
    page: int
    label: str = ""
    bbox: list[float] | None = None
    coordinate_system: str | None = None


class NormativeCheck(DTO):
    label: str
    state: Literal["match", "mismatch", "unknown"]
    actual: str | None = None
    expected: str | None = None
    norm_quantity: str | None = None


class NormativeRule(DTO):
    id: str
    title: str
    state: Literal["candidate", "needs_facts", "not_applicable"]
    factor: str | None = None
    target: str = ""
    message: str = ""
    missing_facts: list[str]
    citations: list[NormativeCitation]


class NormativeResource(DTO):
    code: str
    name: str
    unit: str
    quantity: str | None
    quantity_kind: str
    raw_quantity: str
    status: str
    citations: list[NormativeCitation]


class NormativeCandidate(DTO):
    id: str
    code: str
    title: str
    designation: str
    entity_kind: Literal["norm", "clause"]
    text: str = ""
    unit_raw: str = ""
    unit_base: str = ""
    unit_scale: str | None = None
    context_reason: str = ""
    status: Literal["candidate", "needs_facts", "possible_mismatch", "possible_duplicate"]
    extraction_status: str
    edition_status: str
    checks: list[NormativeCheck]
    missing_facts: list[str]
    composition: list[str]
    resources: list[NormativeResource]
    rules: list[NormativeRule]
    citations: list[NormativeCitation]


class NormativeProjectCitation(DTO):
    version_id: str
    filename: str
    page: int
    quote: str
    origin: Literal["machine", "human"]


class NormativeContextAnalysis(DTO):
    responses: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    interpretation: str = ""
    queries: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model: str
    provider: str
    requests: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    elapsed_seconds: float = 0
    trace_id: str | None = None
    trace_url: str | None = None


class NormativeCheckSource(DTO):
    verified_parameters: list[dict[str, str]] = Field(default_factory=list, max_length=16)
    numeric_verified: bool = False
    unit_verified: bool = False
    unit: str = ''
    rects: list[list[float]] = Field(default_factory=list, max_length=40)
    id: str
    kind: Literal["project", "normative"]
    label: str
    page: int
    text: str
    origin: str
    source_id: str | None = None
    version_id: str | None = None
    image_key: str | None = None
    words_key: str | None = None
    engine: str = ""


class NormativePositionCheckItem(DTO):
    required: bool = True
    claim_ids: list[str] = Field(default_factory=list)
    topic: str
    state: Literal["confirmed", "needs_facts", "possible_mismatch"]
    explanation: str
    project_source_ids: list[str]
    normative_source_ids: list[str]


class NormativeNumericCondition(DTO):
    parameter: Literal['quantity', 'thickness']
    operator: Literal['eq', 'lt', 'le', 'gt', 'ge']
    value: str = Field(max_length=40)
    unit: str = Field(max_length=30)
    quote: str = Field(max_length=700)


class NormativeParameterProposal(DTO):
    scope_relation: Literal['same_layer', 'technological_part'] = 'same_layer'
    operation: Literal['direct', 'sum'] = 'direct'
    operand_source_ids: list[str] = Field(default_factory=list, max_length=12)
    scope_state: Literal['verified', 'unresolved'] = 'unresolved'
    scope_quote: str = Field(default='', max_length=900)
    parameter: Literal['quantity', 'thickness', 'coefficient', 'unit_scale']
    value: str = Field(max_length=40)
    unit: str = Field(max_length=30)
    source_id: str = Field(default='', max_length=200)
    quote: str = Field(default='', max_length=900)
    conditions: list[NormativeNumericCondition] = Field(default_factory=list, max_length=6)


class NormativeEvidenceAnchor(DTO):
    source_id: str
    quote: str = ''
    # Fractions of the rendered page (including its PDF rotation), not pixels.
    rects: list[list[float]] = Field(default_factory=list, max_length=40)


class NormativeParameterCard(DTO):
    id: str
    label: str
    role: Literal['project_value', 'coefficient', 'unit_scale', 'calculation']
    row: int | None = None
    declared_value: str
    unit: str
    compared_value: str | None = None
    found_value: str | None = None
    found_unit: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    source_anchors: list[NormativeEvidenceAnchor] = Field(default_factory=list)
    quote: str = ''
    expression: str = ''
    state: Literal['confirmed', 'needs_facts', 'possible_mismatch']
    reason: str
    conditions: list[str] = Field(default_factory=list)
    required: bool = True


class NormativeWorkBasis(DTO):
    quantity: str
    unit: str
    thickness_cm: str
    norm_scale: str
    project_source_ids: list[str]
    normative_source_ids: list[str]
    anchors: dict[str, list[NormativeEvidenceAnchor]] = Field(default_factory=dict)


class QuantityTerm(DTO):
    label: str
    value: str
    rect: list[float]


class QuantityComposition(DTO):
    parent_position_index: int | None = None
    state: Literal['confirmed', 'possible_mismatch']
    unit: str
    total: str
    rounded_total: str
    declared_value: str
    source_id: str
    page: int
    terms: list[QuantityTerm]


class NormativePositionCheck(DTO):
    pipeline_version: int = 1
    decision_origin: Literal['model_with_validated_sources'] | None = None
    execution_state: Literal['complete', 'technical_failure'] = 'complete'
    evidence_trace: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    local_rules_version: int = 0
    quantity_composition: QuantityComposition | None = None
    local_validation_version: int = 0
    local_work_basis: NormativeWorkBasis | None = None
    parameter_schema_version: int = 0
    parameter_cards: list[NormativeParameterCard] = Field(default_factory=list)
    parameter_proposals: list[NormativeParameterProposal] = Field(default_factory=list)
    summary: str
    state: Literal["confirmed", "needs_facts", "possible_mismatch"]
    items: list[NormativePositionCheckItem]
    sources: list[NormativeCheckSource]
    related_positions: list[dict[str, Any]]
    telemetry: NormativeContextAnalysis
    requires_engineer_review: bool = True


class NormativeSnapshot(DTO):
    work_relations: dict[str, Any] = Field(default_factory=dict)
    work_item_id: str
    query: str
    kind: str
    mode: Literal["lexical", "context"] = "lexical"
    context_analysis: NormativeContextAnalysis | None = None
    position_check: NormativePositionCheck | None = None
    facts: NormativeFacts
    fact_origins: dict[str, str]
    kb: dict[str, str]
    vor_version_id: str
    position_key: str
    status: Literal["candidate", "needs_facts", "possible_mismatch", "possible_duplicate"]
    candidates: list[NormativeCandidate]
    missing_facts: list[str]
    checks: list[str]
    project_citations: list[NormativeProjectCitation]
    selected_ids: list[str] = Field(default_factory=list)
    comment: str = ""
    search_id: str = ""
    requires_engineer_review: bool = True


class NormativeReviewDTO(DTO):
    id: str
    kind: Literal["search", "selection", "check"]
    actor: str
    created_at: datetime
    payload: NormativeSnapshot


class NormativeLibrarySource(DTO):
    id: str
    designation: str
    pages: int
    readable_pages: int


class NormativeLibrary(DTO):
    identity: dict[str, str]
    sources: list[NormativeLibrarySource]
    gaps: list[dict[str, str]]
    counts: dict[str, int]


class NormativeStage(DTO):
    status: Literal["pending", "dispatched", "running", "done", "failed", "cancelled"]
    attempts: int
    message: str = ""


class NormativeStateDTO(DTO):
    automatic_stage: NormativeStage | None = None
    profile: Literal["electrical", "roads"] = "electrical"
    library: NormativeLibrary | None = None
    work_name: str
    facts: NormativeFacts
    fact_origins: dict[str, str]
    project_citations: list[NormativeProjectCitation]
    latest_search: NormativeReviewDTO | None = None
    selection: NormativeReviewDTO | None = None
    latest_check: NormativeReviewDTO | None = None


class NormativeWorkbenchSearchDTO(DTO):
    profile: Literal["electrical", "roads"]
    identity: dict[str, str]
    query: str
    candidates: list[NormativeCandidate]


class NormativeVORRowDTO(DTO):
    index: int
    number: str
    work_name: str
    volume: str
    unit: str
    source_code: str = ""
    code_match: bool | None = None
    unit_match: bool | None = None
    name_similarity: int | None = None
    status: Literal["matched", "unit_mismatch", "name_mismatch", "review", "not_found"]
    candidates: list[NormativeCandidate]


class NormativeVORCheckDTO(DTO):
    filename: str
    profile: Literal["electrical", "roads"]
    total_positions: int
    returned_positions: int
    truncated: bool
    rows: list[NormativeVORRowDTO]


class NormativeReleaseDTO(DTO):
    release_id: str
    database: str
    observed_on: str = ""
    parser_version: str = ""
    current: bool = False
    counts: dict[str, int]
    integrity: str = "unknown"
    foreign_keys: str = "unknown"
    engineering_validation: str = "not_full"


class NormativeSourceAdminDTO(DTO):
    id: str
    designation: str
    sha256: str | None = None
    kind: str = ""
    role: str = ""
    pages: int
    readable_pages: int


class NormativeReviewIssueDTO(DTO):
    id: str
    source_id: str = ""
    designation: str = ""
    entity_id: str = ""
    reason: str
    page: int | None = None


class NormativeCorpusAdminDTO(DTO):
    profile: Literal["electrical", "roads"]
    title: str
    identity: dict[str, str]
    counts: dict[str, int]
    releases: list[NormativeReleaseDTO]
    sources: list[NormativeSourceAdminDTO]
    gaps: list[dict[str, Any]]
    review_queue: list[NormativeReviewIssueDTO]
    review_queue_total: int


class NormativeCatalogFileDTO(DTO):
    id: str
    designation: str
    size_bytes: int
    sha256: str
    imported: bool


class NormativeCatalogSectionDTO(DTO):
    title: str
    files: list[NormativeCatalogFileDTO]


class NormativeCatalogTypeDTO(DTO):
    kind: str
    sections: list[NormativeCatalogSectionDTO]
    file_count: int


class NormativeProfileErrorDTO(DTO):
    profile: Literal["electrical", "roads"]
    message: str


class NormativeManagerDTO(DTO):
    profiles: list[NormativeCorpusAdminDTO]
    collections: list[NormativeCatalogTypeDTO]
    profile_errors: list[NormativeProfileErrorDTO]

class ProjectDTO(DTO):
    id: str
    name: str
    archived: bool = False
    created_at: datetime | None = None


class RunCountDTO(DTO):
    count: int


class DepartmentDTO(DTO):
    id: str
    project_id: str
    name: str
    icon: str
    is_system: bool = False
    created_at: datetime | None = None
    vor_count: int = 0
    pdf_count: int = 0
    run_count: int = 0
    running_count: int = 0


class DepartmentCreate(DTO):
    name: str = Field(min_length=1, max_length=256)


class DepartmentUpdate(DTO):
    name: str = Field(min_length=1, max_length=256)


class ProjectCreate(DTO):
    name: str = Field(min_length=1, max_length=256)




class ProjectUpdate(DTO):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    archived: bool | None = None


class MetadataSourceDTO(DTO):
    sheet: str = ""
    cell: str = ""
    raw: str = ""


class DocumentMetadataDTO(DTO):
    construction_title: str = ""
    raw_title: str = ""
    document_code: str = ""
    pd_reference: str = ""
    sources: dict[str, MetadataSourceDTO] = Field(default_factory=dict)
    extractor_version: str = ""


class VersionDTO(DTO):
    id: str
    project_id: str
    department_id: str | None = None
    document_id: str
    filename: str
    kind: Literal["vor", "pdf"]
    number: int
    sha256: str
    size: int
    created_at: datetime
    metadata: DocumentMetadataDTO
    metadata_overrides: dict[str, str]
    format_status: dict[str, Any] = Field(default_factory=dict)
    title: str = ""


class RunDTO(DTO):
    normative_counts: dict[str, int] = Field(default_factory=dict)
    run_id: str
    project_id: str
    department_id: str | None = None
    vor_version_id: str
    vor_filename: str
    status: Literal["running", "done", "failed", "cancelled"]
    stage: Literal["preparing", "auditing", "finalizing", "done", "failed"]
    total: int | None
    input_position_count: int | None = None
    selected_position_count: int | None = None
    done: int
    failed: int
    running: int
    queued: int
    completion_counts: dict[str, int] = Field(default_factory=dict)
    direction_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    created_at: datetime | None
    finished_at: datetime | None
    preparation_done: int = 0
    preparation_total: int | None = None
    mode: Literal["pipeline", "codex"] = "pipeline"
    codex_stage: str | None = None
    codex_subagents: bool | None = None
    codex_parent_run_id: str | None = None
    codex_metrics: dict[str, Any] | None = None
    codex_error: str | None = None

class DocumentDTO(DTO):
    id: str
    project_id: str
    department_id: str | None = None
    kind: Literal["vor", "pdf"]
    title: str
    archived: bool = False
    current_version_id: str | None = None
    versions: list[VersionDTO]
    last_run: RunDTO | None = None
    pd_updated: bool = False
    vor_updated: bool = False
    run_freshness: Literal["current", "stale", "unavailable"] | None = None


class DocumentPatch(DTO):
    title: str | None = Field(default=None, max_length=2048)
    archived: bool | None = None
    current_version_id: str | None = None


class VersionMetadataPatch(DTO):
    fields: dict[str, str] = Field(default_factory=dict)


class VersionDeletedDTO(DTO):
    deleted: bool


class ResultDTO(DTO):
    id: str
    position_index: int
    number: str
    name: str
    unit: str
    declared_quantity: str
    found_quantity: str | None
    found_unit: str | None
    formula: str
    sheet: str
    source_row: int | None
    cells: dict[str, str]
    audit_status: AuditStatus | None = None
    report_status: ReportStatus | Literal["Проверяется по нормативам"]
    queue: Literal["fix", "review", "missing", "ok", "pending"]
    matching_report_status: ReportStatus | None = None
    related_work: dict[str, Any] | None = None
    problem: str
    action: str
    work_found: bool | None = None
    volume_confirmed: bool | None = None
    unit_confirmed: bool | None = None
    comment_confirmed: bool | None = None
    details: str
    assessment: PositionAssessment | None = None
    result_origin: Literal["graph", "legacy", "codex"] = "legacy"
    codex_conclusion: PositionConclusion | None = None

class RowsDTO(DTO):
    run_id: str
    total: int
    offset: int
    limit: int
    rows: list[ResultDTO]


class SummaryDTO(DTO):
    ready_positions: list[int] = Field(default_factory=list)
    run: RunDTO
    project: ProjectDTO
    queues: dict[str, int]
    documents: list[VersionDTO]
    completion_counts: dict[str, int] = Field(default_factory=dict)
    direction_counts: dict[str, dict[str, int]] = Field(default_factory=dict)

class HighlightDTO(DTO):
    bbox: list[float]
    kind: str
    text: str


class PdfRegionDTO(DTO):
    width: float
    height: float
    highlights: list[HighlightDTO]
    reason: str


class EvidenceDTO(DTO):
    version_id: str
    filename: str
    page: int
    quote: str
    kind: str
    region: PdfRegionDTO | None
    confirmed: bool


class ExcelCellDTO(DTO):
    coordinate: str
    value: str
    column_span: int = 1
    row_span: int = 1
    highlight: bool = False


class ExcelRowDTO(DTO):
    number: int
    selected: bool
    cells: list[ExcelCellDTO]


class ExcelDTO(DTO):
    sheet: str
    columns: list[str]
    rows: list[ExcelRowDTO]


class WorkspaceDTO(DTO):
    row: ResultDTO
    position: VorPosition
    vor_version_id: str
    excel: ExcelDTO | None
    evidence: list[EvidenceDTO]


class ConfirmBasisRequest(DTO):
    evidence_index: int = Field(ge=0)
    comment: str = Field(default="", max_length=2000)


class BasisFragmentRequest(DTO):
    page: int = Field(ge=1)
    rect: list[float] = Field(min_length=4, max_length=4)
    corrected_text: str | None = Field(default=None, max_length=4000)
    text_checked: bool = False
    role: Literal["work", "volume", "unit", "conditions", "other"] = "other"

    @field_validator("rect")
    @classmethod
    def _rectangle(cls, rect):
        import math
        if any(not math.isfinite(n) or not 0 <= n <= 1 for n in rect) or rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError("Укажите непустую область с координатами от 0 до 1")
        return rect


class BasisFragmentDTO(BasisFragmentRequest):
    region: PdfRegionDTO
    extracted_text: str = ""
    ocr_text: str | None = None
    text: str = ""
    image_key: str | None = None


class SpecifyBasisRequest(DTO):
    version_id: str
    page: int = Field(ge=1)
    rect: list[float] = Field(min_length=4, max_length=4)
    comment: str = Field(default="", max_length=2000)
    mode: Literal["row", "area", "text", "multi"] = "area"
    rects: list[list[float]] | None = Field(default=None, min_length=1, max_length=200)
    selection_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fragments: list[BasisFragmentRequest] = Field(default_factory=list, max_length=20)
    ocr_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$")

    @field_validator("rect")
    @classmethod
    def _rectangle(cls, rect):
        import math
        if any(not math.isfinite(n) or not 0 <= n <= 1 for n in rect):
            raise ValueError("Координаты должны быть конечными числами от 0 до 1")
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError("Выделите непустую область")
        return rect

    @field_validator("rects")
    @classmethod
    def _rectangles(cls, rects):
        if rects is not None:
            for rect in rects:
                if len(rect) != 4:
                    raise ValueError("Укажите четыре координаты для каждого фрагмента")
                cls._rectangle(rect)
        return rects


class BasisPreviewDTO(DTO):
    filename: str
    page: int
    quote: str
    rect: list[float]
    region: PdfRegionDTO
    selection_digest: str
    mode: Literal["row", "area", "text", "multi"]
    fragments: list[BasisFragmentDTO] = Field(default_factory=list)
    ocr_key: str | None = None


class HumanBasisDTO(DTO):
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    result_version_id: str | None = None
    outdated: bool = False
    ocr_key: str | None = None
    fragments: list[BasisFragmentDTO] = Field(default_factory=list)
    id: str
    run_id: str
    position_index: int
    pdf_version_id: str
    filename: str
    page: int
    quote: str
    rect: list[float] | None
    region: PdfRegionDTO | None
    origin: Literal["confirmed", "specified"]
    comment: str
    actor: str
    created_at: datetime
    reused: bool


class StructureNodeDTO(DTO):
    model_config = ConfigDict(extra="ignore")
    id: str
    parent_id: str
    title: str
    kind: Literal["section", "appendix", "table"]
    number: str | None
    level: int
    physical_start: int
    physical_end: int
    start_bbox: list[float]
    children: list[str]
    confidence: Literal["high", "medium"]
    sources: list[str]


class StructurePageDTO(DTO):
    model_config = ConfigDict(extra="ignore")
    physical_page: int
    printed_page: str | None
    width: float
    height: float
    node_ids: list[str]


class StructureDTO(DTO):
    model_config = ConfigDict(extra="ignore")
    sha256: str
    page_count: int
    roots: list[str]
    nodes: list[StructureNodeDTO]
    pages: list[StructurePageDTO]
    quantity_confirmation: Literal[False] = False
    ocr_dispatched: Literal[False] = False


class FormatIssueDTO(DTO):
    coord: str
    type: str
    expected: str
    actual: str
    status: str
    error: str
    group: str = ""
    group_title: str = ""
    group_hint: str = ""


class FormatDTO(DTO):
    version_id: str
    sheet: str = ""
    sheets: list[str] = Field(default_factory=list)
    position_count: int = 0
    check_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    issues: list[FormatIssueDTO]


class NoteDTO(DTO):
    id: str
    page: int
    rect: list[float]
    text: str
    deleted: bool
    position_id: str


class PdfBindingsDTO(DTO):
    references: list[str]
    bindings: dict[str, str]


class PdfBindingsPatch(DTO):
    bindings: dict[str, str] = Field(default_factory=dict, max_length=500)


class ReviewArtifactDTO(DTO):
    id: str
    kind: Literal["draft", "ocr"]
    actor: str
    created_at: datetime
    selection: SpecifyBasisRequest
    preview: BasisPreviewDTO


class ResultVersionDTO(DTO):
    id: str
    created_at: datetime
    status: str
    found_volume: str
    found_unit: str
    details: str


class ReviewHistoryDTO(DTO):
    results: list[ResultVersionDTO]
    artifacts: list[ReviewArtifactDTO]
    bases: list[HumanBasisDTO]


class PreparedPageDTO(DTO):
    page: int
    text: str
    pending: bool
    origin: str
    quality: dict[str, Any] | None = None
    error: str | None = None
    blob_key: str
    sha256: str
    truncated: bool
