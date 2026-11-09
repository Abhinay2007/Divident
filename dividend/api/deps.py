"""Request-scoped dependencies."""

import hashlib
import hmac

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..errors import PaymentProviderError


async def get_db(request: Request):
    factory = request.app.state.session_factory
    db = factory()
    try:
        yield db
        if db.in_transaction():
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def get_payment_service(request: Request):
    service = getattr(request.app.state, "payment_service", None)
    if service is None:
        raise PaymentProviderError("Payment provider is unavailable", code="payment_provider_unavailable")
    return service


async def require_plan_access(
    plan_id: str, request: Request,
    x_plan_token: str | None = Header(default=None, alias="X-Plan-Token"),
):
    expected = plan_access_token(request.app.state.settings.ADMIN_TOKEN, plan_id)
    if not x_plan_token or not hmac.compare_digest(expected, x_plan_token):
        raise HTTPException(status_code=401, detail={"code": "plan_auth_required"})


def plan_access_token(admin_token: str, plan_id: str) -> str:
    return hmac.new(admin_token.encode(), f"plan:{plan_id}".encode(), hashlib.sha256).hexdigest()


async def admin_auth(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    expected = request.app.state.settings.ADMIN_TOKEN
    if not x_admin_token or not hmac.compare_digest(expected, x_admin_token):
        raise HTTPException(status_code=401, detail={"code": "admin_auth_required"})
