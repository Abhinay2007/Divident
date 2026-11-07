"""Request-scoped database dependency."""

from fastapi import Request
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
