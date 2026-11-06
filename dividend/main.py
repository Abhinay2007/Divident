"""FastAPI application factory and database startup wiring."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import models  # noqa: F401 - register all ORM metadata before startup
from .config import Settings, get_settings
from .db import create_database_engine, create_session_factory, initialize_database
from .errors import (
    ConfigurationError, DividendError, InvariantViolation, LLMDisabled,
    LLMSchemaViolation, NotFoundError, PaymentProviderError, RateLimitExceeded,
    StateError, ValidationError,
)
from .api.middleware import RequestContextMiddleware
from .api.routes_plans import router as plans_router
from .api.routes_sessions import router as sessions_router
from .schemas import HealthResponse
from .enrichment.fixtures import FixtureEnricher
from .enrichment.github_client import GitHubClient
from .enrichment.live import LiveEnricher
from .enrichment.udd_client import UDDClient
from .upstream.overrides import load_overrides
from .services import SessionService


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
    session_factory=None,
    resolution_runner=None,
) -> FastAPI:
    """Build an application with injectable persistence and T4 resolution seam."""
    configured = settings or get_settings()
    owns_engine = engine is None
    app_engine = engine or create_database_engine(configured.DATABASE_URL)
    factory = session_factory or create_session_factory(app_engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(app_engine)
        yield
        if owns_engine:
            app_engine.dispose()

    app = FastAPI(title="Dividend", lifespan=lifespan)
    app.state.settings = configured
    app.state.engine = app_engine
    app.state.session_factory = factory
    if resolution_runner is None and owns_engine and configured.ENRICH_MODE == "fixtures":
        def resolution_runner(session_id: str) -> None:
            with factory() as db:
                try:
                    SessionService.resolve_sources(
                        db, session_id,
                        FixtureEnricher(Path(__file__).parent / "data" / "demo_enrichment.json"),
                        load_overrides(),
                    )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    with factory() as failed_db:
                        SessionService.mark_failed(failed_db, session_id, str(exc)[:500])
                        failed_db.commit()
    elif resolution_runner is None and owns_engine and configured.ENRICH_MODE == "live":
        def resolution_runner(session_id: str) -> None:
            with factory() as db:
                try:
                    SessionService.resolve_sources(
                        db, session_id,
                        LiveEnricher(UDDClient(configured.UDD_DSN), GitHubClient(configured.GITHUB_TOKEN)),
                        load_overrides(),
                    )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    with factory() as failed_db:
                        SessionService.mark_failed(failed_db, session_id, str(exc)[:500])
                        failed_db.commit()
    app.state.resolution_runner = resolution_runner
    app.add_middleware(RequestContextMiddleware)
    app.include_router(sessions_router)
    app.include_router(plans_router)

    def request_id(request: Request) -> str:
        return request.scope.get("state", {}).get("request_id", "")

    @app.exception_handler(ValidationError)
    async def handle_validation_error(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={
            "error": "validation_error", "code": exc.code,
            "detail": exc.detail, "request_id": request_id(request),
        })

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={
            "error": "validation_error", "code": "request_validation_error",
            "detail": {"errors": errors}, "request_id": request_id(request),
        })

    @app.exception_handler(StateError)
    async def handle_state_error(request: Request, exc: StateError):
        return JSONResponse(status_code=409, content={
            "error": "state_error", "current_state": exc.current_state,
            "allowed_actions": exc.allowed_transitions,
            "code": exc.code, "request_id": request_id(request),
        })

    @app.exception_handler(NotFoundError)
    async def handle_not_found(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={
            "error": "not_found", "code": exc.code,
            "request_id": request_id(request),
        })

    @app.exception_handler(DividendError)
    async def handle_dividend_error(request: Request, exc: DividendError):
        if isinstance(exc, (PaymentProviderError, LLMSchemaViolation)):
            status_code, error_name = 502, "payment_provider_error"
        elif isinstance(exc, RateLimitExceeded):
            status_code, error_name = 429, "rate_limited"
        elif isinstance(exc, LLMDisabled):
            status_code, error_name = 503, "llm_disabled"
        elif isinstance(exc, InvariantViolation):
            status_code, error_name = 500, "internal_error"
        elif isinstance(exc, ConfigurationError):
            status_code, error_name = 500, "configuration_error"
        else:
            status_code, error_name = 500, "internal_error"
        content = {"error": error_name, "code": exc.code, "request_id": request_id(request)}
        if status_code == 429 and getattr(exc, "retry_after_seconds", None) is not None:
            content["retry_after_seconds"] = exc.retry_after_seconds
        if status_code == 503:
            content["message"] = str(exc)
        if isinstance(exc, PaymentProviderError) and exc.retry_hint is not None:
            content["retry_hint"] = exc.retry_hint
        return JSONResponse(status_code=status_code, content=content)

    @app.exception_handler(Exception)
    async def handle_unknown_error(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={
            "error": "internal_error", "request_id": request_id(request),
        })

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health():
        with app_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return HealthResponse(
            db="ok",
            provider_mode=configured.PAYMENTS_MODE,
            fixtures_loaded=(Path(__file__).parent / "data" / "demo_dpkg.tsv").exists(),
        )

    return app
