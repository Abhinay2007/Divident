"""Public API request and response contracts from the LLD."""

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SOURCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreateRequest(Contract):
    mode: Literal["demo", "upload", "paste"]
    payload: dict[str, Any] | str | None = None

    @model_validator(mode="after")
    def validate_payload_for_mode(self):
        if self.mode == "demo" and self.payload is not None:
            raise ValueError("demo payload must be absent")
        if self.mode == "paste" and (not isinstance(self.payload, str) or not self.payload):
            raise ValueError("paste payload must be newline-separated text")
        if self.mode == "upload":
            if not isinstance(self.payload, dict) or set(self.payload) != {"schema", "origin", "binary_count", "source_count", "sources"}:
                raise ValueError("upload payload must match scan.json shape")
            if (type(self.payload["schema"]) is not int or self.payload["schema"] != 1
                    or type(self.payload["origin"]) is not str or not self.payload["origin"].strip()
                    or type(self.payload["binary_count"]) is not int or self.payload["binary_count"] < 0
                    or type(self.payload["source_count"]) is not int or self.payload["source_count"] < 0
                    or not isinstance(self.payload["sources"], dict)):
                raise ValueError("upload payload must match scan.json shape")
            sources = self.payload["sources"]
            if len(sources) > 5000:
                raise ValueError("source count must not exceed 5000")
            if self.payload["source_count"] != len(sources):
                raise ValueError("source_count must match sources")
            binary_names: set[str] = set()
            for source, binaries in sources.items():
                if not isinstance(source, str) or not SOURCE_NAME_RE.fullmatch(source):
                    raise ValueError("source names must match the Debian package-name pattern")
                if not isinstance(binaries, list) or not all(
                    isinstance(binary, str) and SOURCE_NAME_RE.fullmatch(binary)
                    for binary in binaries
                ):
                    raise ValueError("scan sources must map source names to binary-name lists")
                if len(binaries) != len(set(binaries)):
                    raise ValueError("binary names within a source must be unique")
                if binary_names.intersection(binaries):
                    raise ValueError("a binary package cannot belong to multiple sources")
                binary_names.update(binaries)
            if self.payload["binary_count"] != len(binary_names):
                raise ValueError("binary_count must match source binary membership")
        return self


class SessionResponse(Contract):
    id: str
    mode: str
    status: str
    stage: str | None
    progress_done: int
    progress_total: int
    source_count: int
    binary_count: int | None
    error_message: str | None


class WorkspacePlanRef(Contract):
    id: str
    created_at: datetime
    last_updated_at: datetime


class WorkspaceSession(Contract):
    id: str
    mode: str
    status: str
    source_count: int
    binary_count: int | None
    created_at: datetime
    last_updated_at: datetime
    plans: list[WorkspacePlanRef]


class WorkspaceResponse(Contract):
    sessions: list[WorkspaceSession]


class ProjectResponse(Contract):
    upstream_key: str
    display_name: str
    repository_url: str | None
    validated: bool = False
    evidence: list[dict[str, str]] = Field(default_factory=list)
    binary_count: int
    source_names: list[str]
    need_signals: dict[str, Any]
    payability: str
    reason_summary: str
    maintainer_name: str | None = None


class SourceDetailResponse(Contract):
    source_name: str
    binary_names: list[str] | None
    upstream_key: str | None
    method: str
    outcome: str
    validated: bool = False
    evidence: list[dict[str, str]] | None = None
    repository_url: str | None = None


class ProjectsResponse(Contract):
    projects: list[ProjectResponse]
    unmapped: list[str]
    no_upstream: list[str]
    sources: list[SourceDetailResponse] = Field(default_factory=list)


class PlanCreateRequest(Contract):
    budget_cents: int = Field(ge=100, le=1_000_000, strict=True)
    evaluation_date: date


class PlanBudgetPatchRequest(Contract):
    budget_cents: int = Field(ge=100, le=1_000_000, strict=True)
    evaluation_date: date


class PlanLineResponse(Contract):
    upstream_key: str
    display_name: str
    amount_cents: int = Field(strict=True)
    payable_cents: int = Field(strict=True)
    reserved_cents: int = Field(strict=True)
    payability: str
    reason: str
    facts_snapshot: dict[str, Any] | None
    maintainers: list["MaintainerAllocationResponse"] = Field(default_factory=list)


class MaintainerAllocationResponse(Contract):
    maintainer_id: str
    display_name: str
    share_bps: int = Field(strict=True)
    amount_cents: int = Field(strict=True)


class MoneySummary(Contract):
    charged_now_cents: int | None = Field(strict=True)
    pledged_cents: int = Field(strict=True)
    fee_estimate_cents: int | None = Field(strict=True)


class PlanResponse(Contract):
    id: str
    session_id: str
    state: str
    budget_cents: int = Field(strict=True)
    payable_total_cents: int = Field(strict=True)
    reserved_total_cents: int = Field(strict=True)
    est_payout_fees_cents: int | None = Field(strict=True)
    est_incoming_fee_cents: int | None = Field(default=None, strict=True)
    fee_policy_version: str | None = None
    charge_cents: int | None = Field(strict=True)
    order_id: str | None
    order_attempt: int
    capture_attempt: int
    payout_attempt: int
    capture_gross_cents: int | None
    capture_fee_cents: int | None
    capture_net_cents: int | None
    failure_reason: str | None
    frozen_at: datetime | None
    lines: list[PlanLineResponse]
    money_summary: MoneySummary
    action_token: str | None = None


class EvidenceReviewContributor(Contract):
    contributor_id: str
    observation: str
    cited_pr_numbers: list[int] = Field(default_factory=list, strict=True)
    merged_pr_count: int = Field(strict=True)
    reviewed_pr_count: int = Field(strict=True)


class EvidenceReviewCitation(Contract):
    contributor_id: str
    activity_type: Literal["merged_pull_request", "code_review"]
    pr_number: int = Field(strict=True)
    url: str


class EvidenceReviewResponse(Contract):
    status: Literal["available", "unavailable", "invalid"]
    reason: str | None = None
    summary: str | None = Field(default=None, min_length=1, max_length=1500, pattern=r"\S")
    contributors: list[EvidenceReviewContributor] = Field(default_factory=list)
    cited_pr_numbers: list[int] = Field(default_factory=list, strict=True)
    citations: list[EvidenceReviewCitation] = Field(default_factory=list)
    advisory_flags: list[str] = Field(default_factory=list)
    proposal_sha256: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    retry_after_seconds: float | None = None


class ApproveResponse(Contract):
    order_id: str
    approval_url: str


class CaptureDetail(Contract):
    capture_id: str
    gross_cents: int = Field(strict=True)
    fee_cents: int | None = Field(default=None, strict=True)
    net_cents: int = Field(strict=True)
    currency: str
    status: str


class CaptureResponse(Contract):
    capture: CaptureDetail
    state: str


class PayoutResponse(Contract):
    batch_id: str
    item_count: int
    state: str


class ItemResponse(Contract):
    sender_item_id: str
    upstream_key: str
    maintainer_id: str | None = None
    recipient_display: str = "Verified recipient"
    amount_cents: int = Field(strict=True)
    status: str
    failure_reason: str | None = None
    updated_at: datetime


class BatchResponse(Contract):
    id: str
    attempt: int
    status: str
    fees_cents: int | None = Field(strict=True)
    items: list[ItemResponse]


class PayoutsResponse(Contract):
    batches: list[BatchResponse]


class LedgerEventResponse(Contract):
    id: str
    type: str
    plan_id: str | None
    session_id: str | None
    external_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class LedgerResponse(Contract):
    events: list[LedgerEventResponse]
    next_cursor: str | None


class HealthResponse(Contract):
    db: str
    provider_mode: str
    fixtures_loaded: bool


class ClaimInvitationRequest(Contract):
    token: str = Field(min_length=32, max_length=128)


class ClaimStatusResponse(Contract):
    status: Literal["invited", "pending", "verified"]
    github_login: str | None
    github_user_id: int | None = Field(strict=True)
    created_at: datetime
    verified_at: datetime | None


class ClaimInvitationResponse(Contract):
    claim_url: str
    expires_in_seconds: int


class FeeParams(Contract):
    order_fee_pct: float | None
    order_fee_fixed_cents: int | None = Field(strict=True)
    payout_fee_per_item_cents: int | None = Field(strict=True)
    buffer_cents: int | None = Field(strict=True)
    tolerance_cents: int = Field(strict=True)


class ConfigResponse(Contract):
    budget_min_cents: int
    budget_max_cents: int
    fee_params: FeeParams
    paypal_env: str
    sandbox: bool
    payments_mode: Literal["paypal", "fake"]
    llm_live_enabled: bool
    cold_start_seconds: int


class ResolveLiveRequest(Contract):
    source_names: list[str] = Field(min_length=1, max_length=5000)

    @field_validator("source_names")
    @classmethod
    def validate_source_names(cls, values: list[str]) -> list[str]:
        if any(not SOURCE_NAME_RE.fullmatch(value) for value in values):
            raise ValueError("source names must match the Debian package-name pattern")
        return values


class ResolveLiveResponse(Contract):
    resolved: int
    unmapped_remaining: int
    cached: bool


class ExplanationClaimResponse(Contract):
    id: str
    text: str
    evidence_ids: list[str]


class ProjectExplanationResponse(Contract):
    upstream_key: str
    status: str
    claims: list[ExplanationClaimResponse]
    evidence: list[dict[str, Any]]


class ExplanationsResponse(Contract):
    plan_id: str
    explanations: list[ProjectExplanationResponse]
