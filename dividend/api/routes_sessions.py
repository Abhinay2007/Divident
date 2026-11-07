"""Session intake and discovery read routes."""

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session as DbSession

from ..models import Session as SessionRecord
from ..schemas import ProjectsResponse, SessionCreateRequest, SessionResponse
from ..services import SessionService
from .deps import get_db


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _session_response(session: SessionRecord) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        mode=session.mode,
        status=session.status,
        stage=session.stage,
        progress_done=session.progress_done,
        progress_total=session.progress_total,
        source_count=session.source_count,
        binary_count=session.binary_count,
        error_message=session.error_message,
    )


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    request: SessionCreateRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    session = SessionService.create(
        db,
        request.mode,
        request.payload,
        background_tasks=background_tasks,
        resolution_runner=getattr(http_request.app.state, "resolution_runner", None),
    )
    # The background resolver opens a separate DB session and must see the new row.
    db.commit()
    return _session_response(session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, db: DbSession = Depends(get_db)):
    return _session_response(SessionService.get(db, session_id))


@router.get("/{session_id}/projects", response_model=ProjectsResponse)
async def get_projects(session_id: str, db: DbSession = Depends(get_db)):
    return SessionService.get_projects(db, session_id)
