"""Invitation-authenticated GitHub identity claims."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from ..schemas import ClaimInvitationRequest, ClaimInvitationResponse, ClaimStatusResponse
from ..services.maintainer_claim_service import ClaimError, MaintainerClaimService
from .deps import admin_auth, get_db

router = APIRouter(prefix="/api", tags=["maintainer claims"])
_COOKIE = "dividend_maintainer_session"


def _session_hash(request: Request) -> str | None:
    from ..services.maintainer_claim_service import _hash
    token = request.cookies.get(_COOKIE)
    return _hash(token) if token else None


def _same_origin(request: Request) -> None:
    configured = request.app.state.settings
    allowed = {str(configured.PUBLIC_BASE_URL).rstrip("/")}
    allowed.update(str(origin).rstrip("/") for origin in configured.CORS_ORIGINS)
    if request.headers.get("origin", "").rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail={"code": "claim_origin_invalid"})


def _service(request: Request) -> MaintainerClaimService:
    return request.app.state.maintainer_claim_service


@router.post("/admin/maintainer-claims/{recipient_id}/invite",
             response_model=ClaimInvitationResponse, dependencies=[Depends(admin_auth)])
async def issue_claim_invitation(recipient_id: str, request: Request,
                                 db: DbSession = Depends(get_db)):
    service = _service(request)
    try:
        url = service.issue_invitation(db, recipient_id)
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return ClaimInvitationResponse(claim_url=url, expires_in_seconds=7 * 24 * 60 * 60)


@router.post("/maintainer-claims/session", response_model=ClaimStatusResponse)
async def redeem_claim_invitation(body: ClaimInvitationRequest, request: Request,
                                  response: Response, db: DbSession = Depends(get_db)):
    _same_origin(request)
    service = _service(request)
    try:
        token, status = service.redeem_invitation(db, body.token)
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    response.set_cookie(
        _COOKIE, token, max_age=12 * 60 * 60, httponly=True,
        secure=str(request.app.state.settings.PUBLIC_BASE_URL).startswith("https://"),
        samesite="lax", path="/api/maintainer-claims",
    )
    return status


@router.get("/maintainer-claims/me", response_model=ClaimStatusResponse)
async def get_claim_status(request: Request, db: DbSession = Depends(get_db)):
    try:
        return _service(request).status(db, _session_hash(request))
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/maintainer-claims/oauth/start")
async def start_github_oauth(request: Request, db: DbSession = Depends(get_db)):
    _same_origin(request)
    try:
        url = _service(request).authorization_url(db, _session_hash(request))
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return {"authorization_url": url}


@router.get("/maintainer-claims/oauth/callback")
async def github_oauth_callback(request: Request, db: DbSession = Depends(get_db)):
    callback = request.scope.get("github_oauth_callback", {})
    try:
        result = await _service(request).callback(
            db, _session_hash(request), callback.get("state"),
            callback.get("code"), callback.get("error"),
        )
    except ClaimError:
        result = "authorization_failed"
    response = RedirectResponse(
        f"{str(request.app.state.settings.PUBLIC_BASE_URL).rstrip('/')}/claim?result={result}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
