"""FastAPI application factory and database startup wiring."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import models  # noqa: F401 - register all ORM metadata before startup
from .allocation.engine import AllocationError
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
from .api.routes_workspace import router as workspace_router
from .api.routes_maintainer_claims import router as maintainer_claims_router
from .schemas import ConfigResponse, HealthResponse
from .enrichment.fixtures import FixtureEnricher
from .enrichment.github_client import GitHubClient
from .enrichment.live import LiveEnricher
from .enrichment.udd_client import UDDClient
from .upstream.overrides import load_overrides
from .services import EvidenceReviewService, SessionService
from .payments.fake import FakeProvider
from .payments.paypal import PayPalProvider
from .llm.modal_provider import ModalProvider
from .llm.limits import ModalRequestBudget
from .services.payment_service import PaymentService
from .services.config_service import ConfigService
from .services.maintainer_claim_service import ClaimError, MaintainerClaimService
from .demo import seed_demo_if_empty


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
    session_factory=None,
    resolution_runner=None,
    payment_provider=None,
    llm_provider=None,
) -> FastAPI:
    """Build an application with injectable persistence and T4 resolution seam."""
    configured = settings or get_settings()
    owns_engine = engine is None
    app_engine = engine or create_database_engine(configured.DATABASE_URL)
    factory = session_factory or create_session_factory(app_engine)
    owns_provider = payment_provider is None and configured.PAYMENTS_MODE == "paypal"
    provider = payment_provider
    if provider is None and configured.PAYMENTS_MODE == "fake":
        provider = FakeProvider()
    elif provider is None and configured.PAYMENTS_MODE == "paypal":
        provider = PayPalProvider(configured.PAYPAL_CLIENT_ID or "",
                                  configured.PAYPAL_CLIENT_SECRET or "")
    llm_enabled = configured.LLM_LIVE_ENABLED and not configured.LLM_KILL_SWITCH
    owns_llm = llm_provider is None and llm_enabled
    review_provider = llm_provider if llm_enabled else None
    if review_provider is None and llm_enabled:
        review_provider = ModalProvider(
            configured.MODAL_MODEL,
            budget=ModalRequestBudget(factory),
            endpoint_url=str(configured.MODAL_ENDPOINT_URL),
            proxy_token_id=configured.MODAL_PROXY_TOKEN_ID,
            proxy_token_secret=configured.MODAL_PROXY_TOKEN_SECRET,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(app_engine)
        if configured.DEMO_SEED_ON_BOOT:
            seed_demo_if_empty(factory, configured.DEMO_SANDBOX_RECIPIENTS_FILE)
        try:
            yield
        finally:
            if owns_provider:
                provider.close()
            if owns_llm:
                review_provider.close()
            if owns_engine:
                app_engine.dispose()

    app = FastAPI(title="Dividend", lifespan=lifespan)
    app.state.settings = configured
    app.state.engine = app_engine
    app.state.session_factory = factory
    app.state.maintainer_claim_service = MaintainerClaimService(
        configured.GITHUB_OAUTH_CLIENT_ID, configured.GITHUB_OAUTH_CLIENT_SECRET,
        str(configured.PUBLIC_BASE_URL),
    )
    app.state.provider = provider
    app.state.llm = review_provider
    app.state.evidence_review_service = EvidenceReviewService(
        factory, review_provider, configured.MODAL_MODEL,
    )
    app.state.payment_service = (
        PaymentService(provider, str(configured.PUBLIC_BASE_URL),
                       configured.allocation_policy_path
                       if configured.allocation_policy_path.exists() else None,
                       payments_mode=configured.PAYMENTS_MODE,
                       paypal_env=configured.PAYPAL_ENV)
        if provider is not None else None
    )
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
    app.include_router(workspace_router)
    app.include_router(plans_router)
    app.include_router(maintainer_claims_router)

    def request_id(request: Request) -> str:
        return request.scope.get("state", {}).get("request_id", "")

    @app.exception_handler(ValidationError)
    async def handle_validation_error(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={
            "error": "validation_error", "code": exc.code,
            "detail": exc.detail, "request_id": request_id(request),
        })

    @app.exception_handler(AllocationError)
    async def handle_allocation_error(request: Request, exc: AllocationError):
        return JSONResponse(status_code=422, content={
            "error": "allocation_error", "code": "allocation_error",
            "detail": str(exc), "request_id": request_id(request),
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

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        return JSONResponse(status_code=exc.status_code, content={
            "error": "authentication_error" if exc.status_code == 401 else "http_error",
            **detail, "request_id": request_id(request),
        })

    @app.exception_handler(ClaimError)
    async def handle_claim_error(request: Request, exc: ClaimError):
        return JSONResponse(status_code=exc.status_code, content={
            "error": "maintainer_claim_error", "code": exc.code,
            "request_id": request_id(request),
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

    @app.get("/config", response_model=ConfigResponse, tags=["config"])
    async def get_config():
        return ConfigService.get(configured)

    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    frontend_index = frontend_dist / "index.html"
    if frontend_index.is_file():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="frontend-assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def serve_frontend(path: str):
            if path == "health" or path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404)
            candidate = (frontend_dist / path).resolve()
            if path and candidate.is_relative_to(frontend_dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_index)

    return app
