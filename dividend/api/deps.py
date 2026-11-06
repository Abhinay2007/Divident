"""Request-scoped database dependency."""

from fastapi import Request
from sqlalchemy.orm import Session


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
