"""Persisted workspace activity index; plan details remain capability-protected."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..models import (LedgerEvent, PayoutBatch, PayoutItem, Plan,
                      Session as SessionRecord, SourceResolution, UpstreamProject)
from ..schemas import WorkspacePlanRef, WorkspaceResponse, WorkspaceSession
from .deps import get_db


router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("", response_model=WorkspaceResponse)
async def get_workspace(db: DbSession = Depends(get_db)):
    sessions = list(db.scalars(
        select(SessionRecord).order_by(SessionRecord.created_at.desc())
    ))
    plans_by_session: dict[str, list[WorkspacePlanRef]] = {session.id: [] for session in sessions}
    latest_session_update = {session.id: session.created_at for session in sessions}
    session_ids = list(plans_by_session)

    def latest_by_session(model, column):
        if not session_ids:
            return {}
        return dict(db.execute(
            select(model.session_id, func.max(column))
            .where(model.session_id.in_(session_ids))
            .group_by(model.session_id)
        ).all())

    for updates in (
        latest_by_session(LedgerEvent, LedgerEvent.created_at),
        latest_by_session(SourceResolution, SourceResolution.created_at),
        latest_by_session(UpstreamProject, UpstreamProject.created_at),
    ):
        for session_id, updated_at in updates.items():
            if updated_at is not None:
                latest_session_update[session_id] = max(latest_session_update[session_id], updated_at)
    plans = list(db.scalars(
        select(Plan).where(Plan.session_id.in_(plans_by_session)).order_by(Plan.created_at.desc())
    )) if plans_by_session else []
    plan_ids = [plan.id for plan in plans]

    def latest_by_plan(model, column):
        if not plan_ids:
            return {}
        return dict(db.execute(
            select(model.plan_id, func.max(column))
            .where(model.plan_id.in_(plan_ids))
            .group_by(model.plan_id)
        ).all())

    ledger_updates = latest_by_plan(LedgerEvent, LedgerEvent.created_at)
    batch_updates = latest_by_plan(PayoutBatch, PayoutBatch.created_at)
    payout_updates = latest_by_plan(PayoutItem, PayoutItem.updated_at)

    for plan in plans:
        timestamps = [plan.created_at, *(updates[plan.id] for updates in
                      (ledger_updates, batch_updates, payout_updates) if updates.get(plan.id) is not None)]
        updated_at = max(timestamps)
        plans_by_session[plan.session_id].append(WorkspacePlanRef(
            id=plan.id, created_at=plan.created_at, last_updated_at=updated_at,
        ))
        latest_session_update[plan.session_id] = max(latest_session_update[plan.session_id], updated_at)

    return WorkspaceResponse(sessions=[WorkspaceSession(
        id=session.id,
        mode=session.mode,
        status=session.status,
        source_count=session.source_count,
        binary_count=session.binary_count,
        created_at=session.created_at,
        last_updated_at=latest_session_update[session.id],
        plans=plans_by_session[session.id],
    ) for session in sessions])
