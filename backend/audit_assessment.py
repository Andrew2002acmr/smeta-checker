"""Single canonical per-position decision contract.

The graph's pure reducer is the only status assigner. Runtime state uses
TypedDict values; only these Pydantic models cross storage/API boundaries.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analysis_replay import digest
from audit_models import ClaimKind, VorPosition
from normative_verification_contract import VERSION as VERIFICATION_RELATIONS_VERSION
from normative_verification_contract import plan as verification_plan


SCHEMA_VERSION = "evidence-assessment-v1"
ENGINE = "langgraph-evidence-v1"
ASSESSMENT_CONTRACT = "assessment-obligations-v1"

DirectionState = Literal[
    "confirmed", "needs_facts", "possible_mismatch", "not_applicable", "pending"
]
ExecutionState = Literal[
    "pending", "running", "complete", "technical_failure", "cancelled"
]
CompletionState = Literal["pending", "complete", "incomplete", "unknown"]
ObligationState = Literal[
    "confirmed", "needs_facts", "possible_mismatch", "not_applicable", "pending"
]
ObligationPolicy = Literal["blocking", "advisory", "not_applicable"]
OwnQuantityState = Literal[
    "confirmed", "consistent_unproven", "conditional", "unresolved"
]
MismatchLevel = Literal["proved", "suspected"] | None


PROJECT_DOMAINS = frozenset(
    {
        "presence",
        "project_property",
        "quantity",
        "unit",
        "thickness",
        "material_specification",
        "project_constraint",
    }
)
NORMATIVE_DOMAINS = frozenset(
    {
        "norm_selection",
        "applicability_condition",
        "coefficient_basis",
        "method_basis",
        "unit_scale_basis",
    }
)
RELATIONSHIP_DOMAINS = frozenset(
    {
        "quantity_subject",
        "calculation",
        "allocation",
        "transfer",
        "workbook_arithmetic",
        "parent_dependency",
    }
)
DOMAIN_BY_DIRECTION = {
    "project": PROJECT_DOMAINS,
    "normative": NORMATIVE_DOMAINS,
    "relationships": RELATIONSHIP_DOMAINS,
}


class AssessmentObligation(BaseModel):
    """One checkable obligation with provenance, not a verdict sentence."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    domain: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=512)
    requirement: str = Field(default="", max_length=1600)
    basis: str = Field(default="", max_length=800)
    required: bool = True
    policy: ObligationPolicy = "blocking"
    state: ObligationState = "pending"
    comparable: bool = True
    mismatch_level: MismatchLevel = None
    fact_ids: list[str] = Field(default_factory=list, max_length=32)
    source_ids: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="", max_length=2000)
    gap: str = Field(default="", max_length=800)

    @field_validator("fact_ids", "source_ids", mode="before")
    @classmethod
    def _stable_refs(cls, value: Any) -> Any:
        return sorted(set(value or []))

    def model_post_init(self, _context: Any) -> None:
        if len(self.id) > 128 or any(char.isspace() for char in self.id):
            raise ValueError("obligation id must be a stable non-space token")
        if self.policy == "not_applicable" and self.state != "not_applicable":
            raise ValueError("not_applicable obligations keep a not_applicable state")
        if (
            self.policy != "not_applicable"
            and self.required is False
            and self.policy != "advisory"
        ):
            raise ValueError("optional obligations are advisory")
        if self.state == "possible_mismatch" and not self.comparable:
            raise ValueError("a non-comparable obligation cannot be a mismatch")
        if self.state == "possible_mismatch" and self.mismatch_level is None:
            raise ValueError("mismatches carry proved/suspected provenance")
        if self.state != "possible_mismatch" and self.mismatch_level is not None:
            raise ValueError("mismatch provenance belongs to mismatches only")


class AssessmentDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DirectionState = "pending"
    obligations: list[AssessmentObligation] = Field(default_factory=list, max_length=64)
    summary: str = Field(default="", max_length=4000)

    def model_post_init(self, _context: Any) -> None:
        ids = [obligation.id for obligation in self.obligations]
        if len(set(ids)) != len(ids):
            raise ValueError("obligation ids are unique within a direction")


class AssessmentFact(BaseModel):
    """Addressable evidence leaf shared across directions."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    parameter: str = Field(min_length=1, max_length=64)
    dimension: str | None = Field(default=None, max_length=32)
    value: str = Field(default="", max_length=64)
    unit: str = Field(default="", max_length=32)
    source_id: str = Field(default="", max_length=256)
    version_id: str | None = Field(default=None, max_length=64)
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(default="", max_length=2400)
    scope_quote: str = Field(default="", max_length=2400)
    operand_source_ids: list[str] = Field(default_factory=list, max_length=12)
    literal_state: Literal["verified", "unresolved"] = "unresolved"
    subject_state: Literal["verified", "unresolved"] = "unresolved"
    calculation_state: Literal["verified", "unresolved", "not_applicable"] = (
        "not_applicable"
    )
    role: str = Field(default="output", max_length=32)

    @field_validator("operand_source_ids", mode="before")
    @classmethod
    def _stable_operands(cls, value: Any) -> Any:
        return sorted(set(value or []))


class AssessmentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    kind: Literal["project", "normative", "workbook"] = "project"
    label: str = Field(default="", max_length=512)
    page: int | None = Field(default=None, ge=1)
    version_id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=12000)
    origin: str = Field(default="", max_length=64)


class OwnQuantity(BaseModel):
    """The row's own declared amount, assessed separately from its inputs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["direct", "derived"] = "direct"
    state: OwnQuantityState = "unresolved"
    unit: str = Field(default="", max_length=32)
    declared_value: str = Field(default="", max_length=64)
    found_value: str | None = Field(default=None, max_length=64)
    found_unit: str | None = Field(default=None, max_length=32)
    operands: list[str] = Field(default_factory=list, max_length=12)
    basis: list[str] = Field(default_factory=list, max_length=12)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    explanation: str = Field(default="", max_length=2000)

    @field_validator("operands", "basis", "conditions", mode="before")
    @classmethod
    def _stable_lists(cls, value: Any) -> Any:
        return sorted(set(value or []))

    def model_post_init(self, _context: Any) -> None:
        if self.state == "confirmed" and (
            self.found_value is None or self.found_unit is None
        ):
            raise ValueError("confirmed own quantity keeps its compared value/unit")


class AssessmentExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ExecutionState = "pending"
    http_attempts: int = Field(default=0, ge=0, le=10_000)
    tool_executions: int = Field(default=0, ge=0, le=100_000)
    stop_reason: str = Field(default="", max_length=512)
    error: str = Field(default="", max_length=1000)


class PositionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["evidence-assessment-v1"] = SCHEMA_VERSION
    engine: Literal["langgraph-evidence-v1"] = ENGINE
    contract: Literal["assessment-obligations-v1"] = ASSESSMENT_CONTRACT
    run_id: str = Field(min_length=1, max_length=64)
    position_index: int = Field(ge=0)
    execution_revision: int = Field(ge=0)
    revision: str = Field(min_length=64, max_length=64)
    fingerprint: dict[str, Any] = Field(default_factory=dict)
    project: AssessmentDirection = Field(default_factory=AssessmentDirection)
    normative: AssessmentDirection = Field(default_factory=AssessmentDirection)
    relationships: AssessmentDirection = Field(default_factory=AssessmentDirection)
    own_quantity: OwnQuantity = Field(default_factory=OwnQuantity)
    facts: list[AssessmentFact] = Field(default_factory=list, max_length=128)
    sources: list[AssessmentSource] = Field(default_factory=list, max_length=96)
    fact_bundle_ref: str = Field(default="", max_length=128)
    source_registry_ref: str = Field(default="", max_length=128)
    open_gaps: list[str] = Field(default_factory=list, max_length=64)
    execution: AssessmentExecution = Field(default_factory=AssessmentExecution)
    completion: CompletionState = "pending"

    @field_validator("open_gaps", mode="before")
    @classmethod
    def _stable_gaps(cls, value: Any) -> Any:
        return sorted(set(value or []))

    def model_post_init(self, _context: Any) -> None:
        if not all(char in "0123456789abcdef" for char in self.revision):
            raise ValueError("revision is a lowercase sha256 hex digest")
        fact_ids = {fact.id for fact in self.facts}
        source_ids = {source.id for source in self.sources}
        for direction in (self.project, self.normative, self.relationships):
            for obligation in direction.obligations:
                unknown_facts = set(obligation.fact_ids) - fact_ids
                unknown_sources = set(obligation.source_ids) - source_ids
                if unknown_facts or unknown_sources:
                    raise ValueError("obligations reference stored facts/sources only")
        if len(fact_ids) != len(self.facts) or len(source_ids) != len(self.sources):
            raise ValueError("facts and sources keep canonical unique ids")
        for fact in self.facts:
            unknown = set(fact.operand_source_ids) - source_ids
            if unknown:
                raise ValueError("fact operands reference stored sources only")
        expected = revision_for(self.model_dump(mode="json", exclude={"revision"}))
        if expected != self.revision:
            raise ValueError("revision does not match the canonical decision payload")


class ObligationInput(TypedDict, total=False):
    id: str
    domain: str
    subject: str
    requirement: str
    basis: str
    required: bool
    policy: ObligationPolicy
    state: ObligationState
    comparable: bool
    mismatch_level: MismatchLevel
    fact_ids: list[str]
    source_ids: list[str]
    reason: str
    gap: str


class DirectionInput(TypedDict, total=False):
    state: DirectionState
    obligations: list[ObligationInput]
    summary: str


class AssessmentInput(TypedDict, total=False):
    run_id: str
    position_index: int
    execution_revision: int
    fingerprint: dict[str, Any]
    project: DirectionInput
    normative: DirectionInput
    relationships: DirectionInput
    own_quantity: dict[str, Any]
    facts: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    fact_bundle_ref: str
    source_registry_ref: str
    open_gaps: list[str]
    execution_state: ExecutionState
    http_attempts: int
    tool_executions: int
    stop_reason: str
    error: str


def _project_source(item: Any) -> dict[str, Any]:
    """Whitelist tool-registry envelopes to the canonical source shape.

    Tool sources carry verification geometry (rects, anchors, OCR
    observations, unit proofs) needed by validators but forbidden in the
    stored assessment. Only identity, location, and citation text persist.
    """
    if isinstance(item, AssessmentSource):
        return item.model_dump(mode="json")
    source = dict(item) if isinstance(item, dict) else {}
    kind = str(source.get("kind", "project"))
    if kind not in {"project", "normative", "workbook"}:
        kind = "project"
    page = source.get("page")
    return {
        "id": str(source.get("id", "")),
        "kind": kind,
        "label": str(source.get("label", ""))[:512],
        "page": int(page) if isinstance(page, int) and page >= 1 else None,
        "version_id": (str(source.get("version_id") or "")[:64] or None),
        "text": str(source.get("text", ""))[:12000],
        "origin": str(source.get("origin", ""))[:64],
    }


def sum_covers_declared(
    declared_value: str,
    declared_unit: str,
    proposals: list[dict[str, Any]],
) -> bool:
    """Decide whether a routed sum equals the position's own declared amount.

    A sum proves the total only when its value matches the declared quantity
    within the same tolerance the quantity cards use (exact, two float ULPs,
    or HALF_UP rounding to the declared precision). A declared share that is
    merely part of a larger sum is not covered: the total never confirms its
    own split. Units must share a dimension; unknown values fail closed.
    """
    from decimal import Decimal, ROUND_HALF_UP

    from normative_parameters import measure, number, same_numeric_result

    expected = measure(declared_value, declared_unit)
    if expected is None:
        return False
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        if (
            proposal.get("parameter") != "quantity"
            or proposal.get("operation") != "sum"
        ):
            continue
        found = measure(proposal.get("value", ""), proposal.get("unit", ""))
        if found is None or expected[0] != found[0]:
            continue
        if expected[1] == found[1] or same_numeric_result(expected[1], found[1]):
            return True
        precision = number(declared_value)
        base_unit = measure("1", declared_unit)
        if precision is None or base_unit is None:
            continue
        normalized = precision.normalize() if precision != 0 else precision
        precision_exp = min(int(normalized.as_tuple().exponent), 0)
        if expected[0] == "count":
            continue
        try:
            rounded = (found[1] / base_unit[1]).quantize(
                Decimal(1).scaleb(precision_exp), rounding=ROUND_HALF_UP
            )
        except Exception:
            continue
        if rounded == normalized:
            return True
    return False


def proof_is_independent(
    own_position_index: int,
    proposals: list[dict[str, Any]],
    sources: list[dict[str, Any]] | list[Any],
) -> bool:
    """Decide whether a sum routed to this position can confirm its own amount.

    A sum ``S = A + B`` proves the total, never the split. The sum may confirm
    the current position only when a ``quantity`` proposal of this position
    explicitly routes it (``operation == "sum"``) and every operand resolves
    to a ``project`` cell through the strict dependency check — never another
    VOR position's amount, a repeated handle, or an unknown source. Absent or
    unroutable sums fail closed.
    """
    if own_position_index < 0 or not proposals:
        return False
    by_id: dict[str, Any] = {}
    for source in sources or []:
        sid = (
            source.get("id")
            if isinstance(source, dict)
            else getattr(source, "id", None)
        )
        if sid:
            by_id[str(sid)] = source
    routed = [
        proposal
        for proposal in proposals
        if isinstance(proposal, dict)
        and proposal.get("parameter") == "quantity"
        and proposal.get("operation") == "sum"
        and proposal.get("source_id") in by_id
    ]
    if not routed:
        return False
    from normative_parent_basis import _proposal_dependencies

    plain = {
        sid: (
            dict(item)
            if isinstance(item, dict)
            else {"id": sid, "kind": getattr(item, "kind", "")}
        )
        for sid, item in by_id.items()
    }
    for proposal in routed:
        try:
            dependencies = _proposal_dependencies(
                {
                    "source_id": proposal.get("source_id"),
                    "operation": "sum",
                    "unit": proposal.get("unit", ""),
                    "value": proposal.get("value", ""),
                    "operand_source_ids": list(
                        proposal.get("operand_source_ids") or []
                    ),
                },
                plain,
            )
        except Exception:
            continue
        if dependencies:
            return True
    return False


def revision_for(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("revision", None)
    text = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _direction_state(
    obligations: list[AssessmentObligation], execution: ExecutionState
) -> DirectionState:
    blocking = [
        obligation
        for obligation in obligations
        if obligation.policy == "blocking" and obligation.required
    ]
    if not blocking:
        if any(obligation.state == "pending" for obligation in obligations):
            return "pending"
        return "not_applicable"
    if any(obligation.state == "possible_mismatch" for obligation in blocking):
        return "possible_mismatch"
    if execution in {"pending", "running"}:
        return "pending"
    if execution in {"technical_failure", "cancelled"}:
        if any(
            obligation.state in {"pending", "needs_facts"} for obligation in blocking
        ):
            return "needs_facts"
        if any(obligation.state == "possible_mismatch" for obligation in blocking):
            return "possible_mismatch"
        return "confirmed"
    if any(obligation.state == "pending" for obligation in blocking):
        return "pending"
    if any(obligation.state == "needs_facts" for obligation in blocking):
        return "needs_facts"
    return "confirmed"


def _completion(
    directions: list[AssessmentDirection],
    execution: ExecutionState,
) -> CompletionState:
    blocking = [
        obligation
        for direction in directions
        for obligation in direction.obligations
        if obligation.policy == "blocking" and obligation.required
    ]
    if execution in {"pending", "running"}:
        return "pending"
    if execution in {"technical_failure", "cancelled"}:
        return "incomplete"
    if any(obligation.state in {"pending", "needs_facts"} for obligation in blocking):
        return "incomplete"
    return "complete"


def reduce_assessment(payload: AssessmentInput) -> PositionAssessment:
    """Build the canonical decision from validated leaf inputs only."""
    execution_state: ExecutionState = payload.get("execution_state", "pending")
    directions: dict[str, AssessmentDirection] = {}
    for name in ("project", "normative", "relationships"):
        raw = payload.get(name, {}) or {}
        obligations = []
        for item in raw.get("obligations", []) or []:
            data = dict(item)
            data.setdefault("required", True)
            data.setdefault(
                "policy", "blocking" if data.get("required", True) else "advisory"
            )
            data.setdefault("state", "pending")
            data.setdefault("comparable", True)
            if data.get("domain") not in DOMAIN_BY_DIRECTION[name]:
                raise ValueError(
                    f"{name} obligation has an unexpected domain: {data.get('domain')}"
                )
            obligations.append(AssessmentObligation.model_validate(data))
        direction = AssessmentDirection(
            state="pending",
            obligations=obligations,
            summary=str(raw.get("summary", "")),
        )
        direction.state = _direction_state(obligations, execution_state)
        directions[name] = direction
    project = directions["project"]
    if not any(
        obligation.id == "project:binding"
        and obligation.required
        and obligation.policy == "blocking"
        for obligation in project.obligations
    ):
        raise ValueError("project keeps a mandatory construction binding")
    facts = [
        AssessmentFact.model_validate(item) for item in payload.get("facts", []) or []
    ]
    sources = [
        AssessmentSource.model_validate(_project_source(item))
        for item in payload.get("sources", []) or []
    ]
    own_quantity = OwnQuantity.model_validate(payload.get("own_quantity", {}) or {})
    execution = AssessmentExecution(
        state=execution_state,
        http_attempts=int(payload.get("http_attempts", 0)),
        tool_executions=int(payload.get("tool_executions", 0)),
        stop_reason=str(payload.get("stop_reason", "")),
        error=str(payload.get("error", "")),
    )
    completion = _completion(
        [project, directions["normative"], directions["relationships"]], execution_state
    )
    open_gaps = sorted(
        {
            obligation.gap or obligation.id
            for direction in (
                project,
                directions["normative"],
                directions["relationships"],
            )
            for obligation in direction.obligations
            if obligation.policy == "blocking"
            and obligation.state in {"pending", "needs_facts"}
        }
    )
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "contract": ASSESSMENT_CONTRACT,
        "run_id": str(payload.get("run_id", "")),
        "position_index": int(payload.get("position_index", 0)),
        "execution_revision": int(payload.get("execution_revision", 0)),
        "fingerprint": dict(payload.get("fingerprint", {}) or {}),
        "project": project.model_dump(mode="json"),
        "normative": directions["normative"].model_dump(mode="json"),
        "relationships": directions["relationships"].model_dump(mode="json"),
        "own_quantity": own_quantity.model_dump(mode="json"),
        "facts": [fact.model_dump(mode="json") for fact in facts],
        "sources": [source.model_dump(mode="json") for source in sources],
        "fact_bundle_ref": str(payload.get("fact_bundle_ref", "")),
        "source_registry_ref": str(payload.get("source_registry_ref", "")),
        "open_gaps": open_gaps or sorted(set(payload.get("open_gaps", []) or [])),
        "execution": execution.model_dump(mode="json"),
        "completion": completion,
    }
    unsigned["revision"] = revision_for(unsigned)
    return PositionAssessment.model_validate(unsigned)


def obligation_ids(position: VorPosition) -> dict[str, list[str]]:
    """Derive stable per-direction obligation IDs from VOR cells only."""
    from audit_core import build_claim_specs

    specs = build_claim_specs(position)
    tasks = verification_plan(position, [spec.claim_id for spec in specs])
    output = {"project": ["project:binding"], "normative": [], "relationships": []}
    for task, spec in zip(tasks, specs, strict=True):
        verification = task.get("verification", {})
        family = verification.get("family")
        claim_id = task["claim_id"]
        if task.get("evidence_task", {}).get("kind") == "normative_condition":
            output["normative"].append(f"normative:condition:{claim_id}")
            continue
        if family == "project_requirement":
            if claim_id not in {"presence", "quantity", "unit"}:
                output["project"].append(f"project:property:{claim_id}")
            elif claim_id == "presence":
                output["project"].append("project:presence")
            elif claim_id == "quantity":
                output["project"].append("project:own_quantity_inputs")
            else:
                output["project"].append("project:unit")
        elif family == "normative_condition":
            output["normative"].append(f"normative:condition:{claim_id}")
        elif family == "calculation_basis":
            output["relationships"].append(f"relationships:basis:{claim_id}")
        elif family == "vor_refinement":
            if spec.kind == ClaimKind.ATTRIBUTE and spec.required:
                output["project"].append(f"project:constraint:{claim_id}")
            else:
                output["normative"].append(f"normative:refinement:{claim_id}")
        else:
            output["project"].append(f"project:unclassified:{claim_id}")
    output["project"].append("project:quantity_subject")
    output["normative"].append("normative:applicability")
    if position.formula or position.calculation:
        output["relationships"].append("relationships:workbook_arithmetic")
    for key in output:
        output[key] = sorted(set(output[key]))
    return output


def assessment_fingerprint(
    *, position: VorPosition, run_config: dict[str, Any], prompt_schema: str
) -> dict[str, Any]:
    payload = {
        "position_key": {
            "index": position.index,
            "work_name": position.work_name,
            "volume": position.volume,
            "unit": position.unit,
            "comment": position.comment,
            "formula": position.formula,
            "position_type": position.position_type,
            "position_source": position.position_source.model_dump(mode="json")
            if position.position_source
            else None,
        },
        "pdf_versions": dict(run_config.get("pdf_versions", {}) or {}),
        "preparation": {
            "contract": run_config.get("document_preparation_version"),
            "extraction": run_config.get("extraction_contract"),
            "corpora": run_config.get("normative_corpora", {}),
        },
        "normative": {
            "profile": run_config.get("normative_profile"),
            "identity": run_config.get("normative_identity", {}),
        },
        "policy": {
            "assessment": ASSESSMENT_CONTRACT,
            "verification_relations": VERIFICATION_RELATIONS_VERSION,
            "rounding": "decimal-exact-input-precision",
        },
        "prompt_schema": prompt_schema,
    }
    return {"sha256": digest(payload), "inputs": payload}
