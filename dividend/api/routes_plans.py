"""Allocation plan read, draft adjustment, creation, and approval routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from ..models import Plan
from ..schemas import (
    ApproveResponse, CaptureResponse, MoneySummary, PlanBudgetPatchRequest,
    PlanCreateRequest, PlanLineResponse, PlanResponse, PayoutResponse,
    PayoutsResponse,
)
from ..services import PlanService
from .deps import get_db, get_payment_service


router = APIRouter(prefix="/api", tags=["plans"])


def _plan_response(plan: Plan) -> PlanResponse:
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
    )


@router.post("/sessions/{session_id}/plans", response_model=PlanResponse, status_code=201)
async def create_plan(
    session_id: str,
    request: PlanCreateRequest,
    db: DbSession = Depends(get_db),
):
    return _plan_response(PlanService.create(
        db, session_id, request.budget_cents, request.evaluation_date
    ))


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(plan_id: str, db: DbSession = Depends(get_db)):
    return _plan_response(PlanService.get(db, plan_id))


@router.patch("/plans/{plan_id}/budget", response_model=PlanResponse)
async def adjust_budget(
    plan_id: str,
    request: PlanBudgetPatchRequest,
    db: DbSession = Depends(get_db),
):
    return _plan_response(PlanService.adjust_budget(
        db, plan_id, request.budget_cents, request.evaluation_date
    ))


@router.post("/plans/{plan_id}/approve", response_model=ApproveResponse)
async def approve_plan(plan_id: str, db: DbSession = Depends(get_db), service=Depends(get_payment_service)):
    return service.approve_and_fund(plan_id, db)


@router.post("/plans/{plan_id}/capture", response_model=CaptureResponse)
async def capture_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    return service.capture(plan_id, db)


@router.post("/plans/{plan_id}/payout", response_model=PayoutResponse)
async def payout_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    return service.payout(plan_id, db)


@router.post("/plans/{plan_id}/payout/retry", response_model=PayoutResponse)
async def retry_payout_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    return service.retry_payout(plan_id, db)


@router.post("/plans/{plan_id}/reconcile", response_model=PayoutsResponse)
async def reconcile_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    return service.reconcile(plan_id, db)


@router.get("/plans/{plan_id}/payouts", response_model=PayoutsResponse)
async def get_payouts(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    return service.get_payouts(plan_id, db)


@router.post("/plans/{plan_id}/cancel", status_code=200)
async def cancel_plan(plan_id: str, service=Depends(get_payment_service), db: DbSession = Depends(get_db)):
    service.cancel(plan_id, db)
    return {"state": "cancelled"}
