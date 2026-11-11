"""Allocation plan read, draft adjustment, creation, and approval routes."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as DbSession

from ..models import Plan
from ..schemas import (
    ApproveResponse, CaptureResponse, MoneySummary, PlanBudgetPatchRequest,
    PlanCreateRequest, PlanLineResponse, PlanResponse, PayoutResponse,
    PayoutsResponse, ExplanationsResponse, EvidenceReviewResponse,
)
from ..services import ExplanationService, PlanService
from .deps import get_db, get_payment_service, plan_access_token, require_plan_access


router = APIRouter(prefix="/api", tags=["plans"])


def _plan_response(plan: Plan, action_token: str | None = None) -> PlanResponse:
    lines = [
        PlanLineResponse(
            upstream_key=line.upstream_key,
            display_name=line.display_name,
            amount_cents=line.amount_cents,
            payable_cents=line.payable_cents,
            reserved_cents=line.reserved_cents,
            payability="payable" if line.payable_cents > 0 else "reserved",
            reason=line.reason,
            facts_snapshot=line.facts_snapshot,
            maintainers=line.maintainer_splits or [],
        )
        for line in plan.lines
    ]
    return PlanResponse(
        id=plan.id,
        session_id=plan.session_id,
        state=plan.state,
        budget_cents=plan.budget_cents,
        payable_total_cents=plan.payable_total_cents,
        reserved_total_cents=plan.reserved_total_cents,
        est_payout_fees_cents=plan.est_payout_fees_cents,
        est_incoming_fee_cents=plan.est_incoming_fee_cents,
        fee_policy_version=plan.fee_policy_version,
        charge_cents=plan.charge_cents,
        order_id=plan.order_id,
        order_attempt=plan.order_attempt,
        capture_attempt=plan.capture_attempt,
        payout_attempt=plan.payout_attempt,
        capture_gross_cents=plan.capture_gross_cents,
        capture_fee_cents=plan.capture_fee_cents,
        capture_net_cents=plan.capture_net_cents,
        failure_reason=plan.failure_reason,
        frozen_at=plan.frozen_at,
        lines=lines,
        money_summary=MoneySummary(
            charged_now_cents=plan.charge_cents,
            pledged_cents=plan.reserved_total_cents,
            fee_estimate_cents=plan.est_payout_fees_cents,
        ),
        action_token=action_token,
    )


@router.post("/sessions/{session_id}/plans", response_model=PlanResponse, status_code=201)
async def create_plan(
    session_id: str,
    request: PlanCreateRequest,
    http_request: Request,
    db: DbSession = Depends(get_db),
):
    policy_path = http_request.app.state.settings.allocation_policy_path
    if not policy_path.exists():
        policy_path = None
    plan = PlanService.create(db, session_id, request.budget_cents,
                              request.evaluation_date, policy_path)
    token = plan_access_token(http_request.app.state.settings.ADMIN_TOKEN, plan.id)
    return _plan_response(plan, token)


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(plan_id: str, db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return _plan_response(PlanService.get(db, plan_id))


@router.get("/plans/{plan_id}/explanations", response_model=ExplanationsResponse)
async def get_plan_explanations(
    plan_id: str, db: DbSession = Depends(get_db),
    _: None = Depends(require_plan_access),
):
    return ExplanationService.get_plan_explanations(db, plan_id)


@router.get("/plans/{plan_id}/evidence-review", response_model=EvidenceReviewResponse)
async def get_plan_evidence_review(
    plan_id: str, request: Request, db: DbSession = Depends(get_db),
    _: None = Depends(require_plan_access),
):
    return request.app.state.evidence_review_service.review(plan_id, db)


@router.patch("/plans/{plan_id}/budget", response_model=PlanResponse)
async def adjust_budget(
    plan_id: str,
    request: PlanBudgetPatchRequest,
    http_request: Request,
    db: DbSession = Depends(get_db),
    _: None = Depends(require_plan_access),
):
    policy_path = http_request.app.state.settings.allocation_policy_path
    if not policy_path.exists():
        policy_path = None
    return _plan_response(PlanService.adjust_budget(
        db, plan_id, request.budget_cents, request.evaluation_date,
        policy_path,
    ))


@router.post("/plans/{plan_id}/approve", response_model=ApproveResponse)
async def approve_plan(plan_id: str, db: DbSession = Depends(get_db), service=Depends(get_payment_service), _: None = Depends(require_plan_access)):
    return service.approve_and_fund(plan_id, db)


@router.post("/plans/{plan_id}/capture", response_model=CaptureResponse)
async def capture_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return service.capture(plan_id, db)


@router.post("/plans/{plan_id}/payout", response_model=PayoutResponse)
async def payout_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return service.payout(plan_id, db)


@router.post("/plans/{plan_id}/payout/retry", response_model=PayoutResponse)
async def retry_payout_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return service.retry_payout(plan_id, db)


@router.post("/plans/{plan_id}/reconcile", response_model=PayoutsResponse)
async def reconcile_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return service.reconcile(plan_id, db)


@router.get("/plans/{plan_id}/payouts", response_model=PayoutsResponse)
async def get_payouts(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    return service.get_payouts(plan_id, db)


@router.post("/plans/{plan_id}/cancel", status_code=200)
async def cancel_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db), _: None = Depends(require_plan_access)):
    service.cancel(plan_id, db)
    return {"state": "cancelled"}
