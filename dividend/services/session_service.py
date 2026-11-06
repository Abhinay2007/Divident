"""Session intake, lifecycle state, and resolved-project read service."""

from collections.abc import Callable
from pathlib import Path
import re

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..errors import NotFoundError, StateError, ValidationError
from ..ledger import append as append_ledger_event
from ..models import Maintainer, Session, SourceResolution, UpstreamProject
from ..scan import group_by_source, parse_rows
from ..schemas import ProjectsResponse, ProjectResponse, SessionCreateRequest


_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")
_DEMO_FIXTURE = Path(__file__).resolve().parents[1] / "data" / "demo_dpkg.tsv"


def _begin_sqlite_transaction(db: DbSession) -> None:
    connection = db.connection()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        # sqlite3 legacy mode does not begin a transaction for SELECT.
        connection.exec_driver_sql("BEGIN")


def _validate_paste(payload: str) -> tuple[list[str], None]:
    names = [line.strip() for line in payload.splitlines() if line.strip()]
    if not names:
        raise ValidationError("paste must contain at least one source package", code="invalid_session_request")
    if len(names) > 5000:
        raise ValidationError("source count must not exceed 5000", code="invalid_session_request")
    if any(not _PACKAGE_NAME.fullmatch(name) for name in names):
        raise ValidationError("source names must match the Debian package-name pattern", code="invalid_session_request")
    if len(set(names)) != len(names):
        raise ValidationError("source names must be unique", code="invalid_session_request")
    return sorted(names), None


class SessionService:
    """Own session intake and lifecycle persistence; resolution stays injected."""

    @staticmethod
    def create(
        db: DbSession,
        mode: str,
        payload: dict | str | None,
        *,
        background_tasks=None,
        resolution_runner: Callable[[str], None] | None = None,
    ) -> Session:
        try:
            request = SessionCreateRequest(mode=mode, payload=payload)
        except PydanticValidationError as exc:
            raise ValidationError(
                "Invalid session request",
                code="invalid_session_request",
                detail={"errors": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]},
            ) from exc

        if request.mode == "upload":
            report = request.payload
            assert isinstance(report, dict)
            source_binaries = {
                source: list(binaries)
                for source, binaries in sorted(report["sources"].items())
            }
            source_names = list(source_binaries)
            binary_count: int | None = report["binary_count"]
        elif request.mode == "paste":
            assert isinstance(request.payload, str)
            source_names, source_binaries = _validate_paste(request.payload)
            binary_count = None
        else:
            rows = parse_rows(_DEMO_FIXTURE.read_text(encoding="utf-8"))
            source_binaries = group_by_source(rows)
            if not source_binaries:
                raise ValidationError("Demo package fixture is empty", code="invalid_session_request")
            source_names = list(source_binaries)
            binary_count = sum(len(binaries) for binaries in source_binaries.values())

        session = Session(
            mode=request.mode,
            status="created",
            source_names=source_names,
            source_binaries=source_binaries,
            binary_count=binary_count,
            source_count=len(source_names),
            progress_done=0,
            progress_total=len(source_names),
        )
        _begin_sqlite_transaction(db)
        with db.begin_nested():
            db.add(session)
            db.flush()
            db.add_all([
                SourceResolution(
                    session_id=session.id,
                    source_name=source_name,
                    upstream_key=None,
                    method="none",
                    evidence=None,
                    validated=False,
                    cached=False,
                )
                for source_name in source_names
            ])
            db.flush()
            append_ledger_event(
                db,
                "SESSION_CREATED",
                {
                    "mode": session.mode,
                    "source_count": session.source_count,
                    "binary_count": session.binary_count,
                },
                session_id=session.id,
            )
            if background_tasks is not None and resolution_runner is not None:
                SessionService._set_resolving(db, session)
                background_tasks.add_task(resolution_runner, session.id)
            db.flush()
        return session

    @staticmethod
    def get(db: DbSession, session_id: str) -> Session:
        session = db.get(Session, session_id) if isinstance(session_id, str) else None
        if session is None:
            raise NotFoundError("Session not found", code="session_not_found")
        return session

    @staticmethod
    def _set_resolving(db: DbSession, session: Session) -> Session:
        if session.status == "resolving":
            return session
        if session.status != "created":
            raise StateError(
                "Session cannot enter resolving from its current state",
                code="invalid_session_transition",
                current_state=session.status,
                allowed_transitions=["resolving"] if session.status == "created" else [],
            )
        session.status = "resolving"
        session.stage = "enriching"
        session.progress_done = 0
        session.progress_total = session.source_count
        return session

    @staticmethod
    def mark_resolving(db: DbSession, session_id: str) -> Session:
        session = SessionService.get(db, session_id)
        if session.status == "resolving":
            return session
        if session.status != "created":
            raise StateError(
                "Session cannot enter resolving from its current state",
                code="invalid_session_transition",
                current_state=session.status,
                allowed_transitions=["resolving"] if session.status == "created" else [],
            )
        _begin_sqlite_transaction(db)
        with db.begin_nested():
            result = db.execute(
                update(Session)
                .where(Session.id == session_id, Session.status == "created")
                .values(
                    status="resolving",
                    stage="enriching",
                    progress_done=0,
                    progress_total=session.source_count,
                )
                .execution_options(synchronize_session="fetch")
            )
            if result.rowcount != 1:
                current = db.scalar(select(Session.status).where(Session.id == session_id))
                raise StateError(
                    "Session state changed before resolution started",
                    code="invalid_session_transition",
                    current_state=current or "missing",
                    allowed_transitions=["resolving"],
                )
        return session

    @staticmethod
    def update_progress(
        db: DbSession,
        session_id: str,
        *,
        stage: str,
        progress_done: int,
        progress_total: int,
    ) -> Session:
        session = SessionService.get(db, session_id)
        if session.status != "resolving":
            raise StateError(
                "Progress can only change while resolving",
                code="invalid_session_transition",
                current_state=session.status,
                allowed_transitions=["resolving"],
            )
        if (
            type(stage) is not str or not stage
            or type(progress_done) is not int or type(progress_total) is not int
            or not 0 <= progress_done <= progress_total
            or progress_total != session.source_count
            or progress_done < session.progress_done
        ):
            raise ValidationError("Invalid session progress", code="invalid_session_progress")
        session.stage = stage
        session.progress_done = progress_done
        session.progress_total = progress_total
        db.flush()
        return session

    @staticmethod
    def mark_failed(db: DbSession, session_id: str, error_message: str) -> Session:
        session = SessionService.get(db, session_id)
        if type(error_message) is not str or not error_message.strip():
            raise ValidationError("error_message must be nonempty", code="invalid_session_error")
        if session.status == "failed" and session.error_message == error_message:
            return session
        if session.status not in {"created", "resolving"}:
            raise StateError(
                "Session cannot fail from its current state",
                code="invalid_session_transition",
                current_state=session.status,
                allowed_transitions=["failed"],
            )
        session.status = "failed"
        session.error_message = error_message
        session.stage = None
        db.flush()
        return session

    @staticmethod
    def get_projects(db: DbSession, session_id: str) -> ProjectsResponse:
        session = SessionService.get(db, session_id)
        projects = list(db.scalars(
            select(UpstreamProject)
            .where(UpstreamProject.session_id == session_id)
            .order_by(UpstreamProject.upstream_key)
        ))
        maintainer_rows = db.execute(
            select(Maintainer.upstream_key, Maintainer.opted_in).where(
                Maintainer.upstream_key.in_([project.upstream_key for project in projects])
            )
        ).all() if projects else []
        opted_in = {key: value for key, value in maintainer_rows}
        resolutions = list(db.scalars(
            select(SourceResolution)
            .where(SourceResolution.session_id == session_id)
            .order_by(SourceResolution.source_name)
        ))
        if type(session.source_names) is not list:
            raise ValidationError("Persisted session source names are invalid", code="invalid_session_data")
        known_sources = set(session.source_names)
        if any(row.source_name not in known_sources for row in resolutions):
            raise ValidationError("Persisted resolution source is outside the session", code="invalid_session_data")
        # The current resolution schema has only method='none' for both
        # unresolved and intentional no-upstream outcomes. Until T4 adds a
        # distinguishing fact, report method='none' as unmapped and never
        # guess the no_upstream category.
        unmapped = sorted(
            row.source_name for row in resolutions
            if row.method == "none" and row.upstream_key is None
        )
        mapped_resolution_names = {row.source_name for row in resolutions}
        unmapped = sorted(set(unmapped) | (known_sources - mapped_resolution_names))
        responses = []
        for project in projects:
            is_payable = opted_in.get(project.upstream_key) is True
            responses.append(ProjectResponse(
                upstream_key=project.upstream_key,
                display_name=project.display_name,
                repository_url=project.repository_url,
                binary_count=project.binary_count,
                source_names=sorted(project.source_names),
                need_signals=project.need_signals,
                payability="payable" if is_payable else "reserved",
                reason_summary=(
                    "Opted-in maintainer is available"
                    if is_payable
                    else "No opted-in maintainer is available"
                ),
            ))
        return ProjectsResponse(projects=responses, unmapped=unmapped, no_upstream=[])
