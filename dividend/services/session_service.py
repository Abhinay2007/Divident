"""Session intake, lifecycle state, and resolved-project read service."""

from collections.abc import Callable
from pathlib import Path
import re
from datetime import date
from time import perf_counter

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from ..errors import NotFoundError, StateError, ValidationError
from ..ledger import append as append_ledger_event
from ..models import Maintainer, Session, SourceResolution, UpstreamProject
from ..allocation.snapshot import source_facts_payload
from ..enrichment.base import Enricher, FactSet, GitHubFacts
from ..upstream.mapper import map_source
from ..upstream.overrides import OverrideConfig
from ..upstream.resolver import LLMProvider, resolve
from ..scan import group_by_source, parse_rows
from ..schemas import ProjectsResponse, ProjectResponse, SessionCreateRequest, SourceDetailResponse


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
                    outcome="unmapped",
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
        unmapped = sorted(
            row.source_name for row in resolutions
            if row.method == "none" and row.upstream_key is None and row.outcome == "unmapped"
        )
        no_upstream = sorted(row.source_name for row in resolutions if row.outcome == "no_upstream")
        mapped_resolution_names = {row.source_name for row in resolutions}
        unmapped = sorted(set(unmapped) | (known_sources - mapped_resolution_names))
        resolution_by_name = {row.source_name: row for row in resolutions}
        sources = [
            SourceDetailResponse(
                source_name=name,
                binary_names=(
                    sorted(session.source_binaries[name])
                    if isinstance(session.source_binaries, dict)
                    and isinstance(session.source_binaries.get(name), list)
                    else None
                ),
                upstream_key=resolution_by_name[name].upstream_key if name in resolution_by_name else None,
                method=resolution_by_name[name].method if name in resolution_by_name else "none",
                outcome=resolution_by_name[name].outcome if name in resolution_by_name else "unmapped",
            )
            for name in sorted(known_sources)
        ]
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
        return ProjectsResponse(projects=responses, unmapped=unmapped, no_upstream=no_upstream, sources=sources)

    @staticmethod
    def resolve_sources(db: DbSession, session_id: str, enricher: Enricher,
                        overrides: OverrideConfig, llm: LLMProvider | None = None) -> Session:
        """Resolve sources safely and upsert projects without duplicate identities."""
        started = perf_counter()
        session = SessionService.get(db, session_id)
        was_ready = session.status == "ready"
        if session.status not in {"created", "resolving", "ready"}:
            raise StateError("Session cannot resolve from its current state", code="invalid_session_transition",
                             current_state=session.status, allowed_transitions=["resolving"])
        names = list(session.source_names)
        facts = enricher.fetch(names)
        fact_by_name = {}
        invalid_names = set()
        for fact in facts:
            if not isinstance(fact, FactSet) or fact.source_name not in names:
                continue
            if fact.source_name in fact_by_name:
                invalid_names.add(fact.source_name)
            else:
                fact_by_name[fact.source_name] = fact
        for name in names:
            fact = fact_by_name.get(name)
            github_valid = fact is None or fact.github is None or (
                isinstance(fact.github, GitHubFacts)
                and type(fact.github.archived) is bool and type(fact.github.funding_yml) is bool
                and (fact.github.last_commit is None or type(fact.github.last_commit) is str)
                and (fact.github.committers_12m is None or (
                    type(fact.github.committers_12m) is int and fact.github.committers_12m >= 0
                ))
            )
            text_valid = fact is None or all(
                value is None or type(value) is str
                for value in (fact.repository_url, fact.homepage, fact.donation, fact.contact,
                              fact.github_owner_login, fact.github_parent_full_name)
            )
            valid_date = True
            if fact and fact.github and fact.github.last_commit:
                try:
                    date.fromisoformat(fact.github.last_commit[:10])
                except ValueError:
                    valid_date = False
            if (name in invalid_names or fact is None or not github_valid or not text_valid
                    or type(fact.partial) is not bool or not valid_date):
                fact_by_name[name] = FactSet(name, partial=True)
        mappings = {name: map_source(name, fact_by_name[name], overrides) for name in names}
        pending = [fact_by_name[name] for name in names if mappings[name].outcome == "unmapped"]
        proposals = resolve(pending, llm, set(overrides.family_patterns or {})) if llm is not None and pending else []
        for proposal in proposals:
            old = mappings[proposal.source_name]
            mappings[proposal.source_name] = type(old)(proposal.source_name, proposal.upstream_key, "llm",
                                                       proposal.evidence, "resolved")

        resolutions = {row.source_name: row for row in db.scalars(
            select(SourceResolution).where(SourceResolution.session_id == session_id))}
        for name, mapping in mappings.items():
            row = resolutions[name]
            if row.method == "llm" and row.validated:
                mappings[name] = type(mapping)(name, row.upstream_key, row.method, row.evidence or [], "resolved")
                continue
            row.upstream_key, row.method, row.outcome = mapping.upstream_key, mapping.method, mapping.outcome
            row.evidence = mapping.evidence or None
            row.validated = mapping.method == "llm"
            row.cached = False

        by_key: dict[str, list[str]] = {}
        for name, mapping in mappings.items():
            if mapping.outcome == "resolved":
                by_key.setdefault(mapping.upstream_key, []).append(name)

        existing = {p.upstream_key: p for p in db.scalars(
            select(UpstreamProject).where(UpstreamProject.session_id == session_id))}
        for stale_key in existing.keys() - by_key.keys():
            db.execute(delete(UpstreamProject).where(
                UpstreamProject.session_id == session_id, UpstreamProject.upstream_key == stale_key
            ))
        for key, source_names in by_key.items():
            selected = [fact_by_name[name] for name in source_names]
            memberships = session.source_binaries or {}
            binary_count = sum(len(memberships[name]) for name in source_names) if session.source_binaries else len(source_names)
            github = [fact.github for fact in selected if fact.github]
            dates = [date.fromisoformat(f.last_commit[:10]) for f in github if f.last_commit]
            repo = next((f.repository_url for f in selected if f.repository_url), None)
            signals = {
                "archived": all(f.archived for f in github) if github else None,
                "last_commit": max(dates).isoformat() if dates else None,
                "committers_12m": sum(f.committers_12m for f in github if f.committers_12m is not None) if github and all(f.committers_12m is not None for f in github) else None,
                "funding_yml": any(f.funding_yml for f in github) if github else None,
                "partial": any(f.partial for f in selected),
            }
            values = dict(display_name=key.rsplit("/", 1)[-1], repository_url=repo,
                          binary_count=binary_count, source_names=sorted(source_names),
                          source_facts=source_facts_payload(selected), need_signals=signals)
            project = existing.get(key)
            if project is None:
                db.add(UpstreamProject(session_id=session_id, upstream_key=key, **values))
            else:
                for field, value in values.items():
                    setattr(project, field, value)
        session.status, session.stage = "ready", "done"
        session.progress_done = session.progress_total = session.source_count
        if not was_ready:
            append_ledger_event(db, "RESOLUTION_COMPLETED", {
                "projects": len(by_key),
                "unmapped": sum(mapping.outcome == "unmapped" for mapping in mappings.values()),
                "no_upstream": sum(mapping.outcome == "no_upstream" for mapping in mappings.values()),
                "duration_ms": max(0, round((perf_counter() - started) * 1000)),
            }, session_id=session_id)
        db.flush()
        return session
