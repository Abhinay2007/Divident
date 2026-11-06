"""Plan-service preparation of immutable allocation inputs."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from ..allocation import (
    AllocationError, FactsSnapshot, ProjectFacts, ProjectInput, allocate,
    load_allocation_policy,
)
from ..allocation.snapshot import build_facts_snapshot
from ..errors import NotFoundError, StateError, ValidationError
from ..ledger import LedgerService, append as append_ledger_event
from ..models import Maintainer, Plan, PlanLine, Session, SourceResolution, UpstreamProject


@dataclass(frozen=True, slots=True)
class AllocationInputSet:
    """Canonical domain inputs and the caller's evaluation date."""

    projects: tuple[ProjectInput, ...]
    evaluation_date: date


class PlanService:
    """Application boundary for preparing inputs to the pure allocator."""

    @staticmethod
    def assemble_allocation_inputs(
        db: DbSession,
        session_id: str,
        evaluation_date: date,
    ) -> AllocationInputSet:
        """Build eligible ProjectInput values without writing to the database.

        Only persisted UpstreamProject records with a matching, resolved
        SourceResolution for every source are eligible. Unmapped and
        no-upstream resolutions have no project record and are excluded.
        """
        if type(evaluation_date) is not date:
            raise AllocationError("evaluation_date must be a date")
        if type(session_id) is not str or not session_id:
            raise AllocationError("session_id must be a nonempty string")

        session = db.get(Session, session_id)
        if session is None:
            raise NotFoundError("Session not found", code="session_not_found")
        if session.status != "ready":
            raise StateError(
                "Session is not ready for allocation",
                code="session_not_ready",
                current_state=session.status,
                allowed_transitions=["ready"],
            )

        projects = list(db.scalars(
            select(UpstreamProject).where(UpstreamProject.session_id == session_id)
        ))
        resolutions = list(db.scalars(
            select(SourceResolution).where(SourceResolution.session_id == session_id)
        ))

        if type(session.source_names) is not list or any(
            type(name) is not str for name in session.source_names
        ):
            raise AllocationError("session source_names must be a list of strings")
        if len(session.source_names) != len(set(session.source_names)):
            raise AllocationError("session source_names contain duplicates")
        session_source_names = set(session.source_names)

        resolution_by_source: dict[str, SourceResolution] = {}
        for resolution in resolutions:
            if resolution.source_name not in session_source_names:
                raise AllocationError(
                    f"resolution source {resolution.source_name!r} is not in the session"
                )
            if resolution.source_name in resolution_by_source:
                raise AllocationError(
                    f"duplicate resolution for source {resolution.source_name!r}"
                )
            if type(resolution.method) is not str or resolution.method not in {
                "override", "repo_url", "llm", "none"
            }:
                raise AllocationError(
                    f"invalid resolution method for source {resolution.source_name!r}"
                )
            if resolution.method == "none":
                if resolution.upstream_key is not None:
                    raise AllocationError(
                        f"no-upstream resolution has an upstream key for {resolution.source_name!r}"
                    )
            elif resolution.upstream_key is None:
                raise AllocationError(
                    f"resolved source has no upstream key for {resolution.source_name!r}"
                )
            elif (
                type(resolution.upstream_key) is not str
                or not resolution.upstream_key
                or resolution.upstream_key != resolution.upstream_key.strip()
            ):
                raise AllocationError(
                    f"invalid upstream key for source {resolution.source_name!r}"
                )
            elif resolution.method == "llm" and resolution.validated is not True:
                raise AllocationError(
                    f"LLM resolution was not validated for {resolution.source_name!r}"
                )
            resolution_by_source[resolution.source_name] = resolution

        if set(resolution_by_source) != session_source_names:
            raise AllocationError("session is missing one or more source resolutions")

        project_by_key: dict[str, UpstreamProject] = {}
        source_owner: dict[str, str] = {}
        for project in projects:
            if project.upstream_key in project_by_key:
                raise AllocationError(f"duplicate upstream project {project.upstream_key!r}")
            project_by_key[project.upstream_key] = project
            if type(project.source_names) is not list or any(
                type(name) is not str for name in project.source_names
            ):
                raise AllocationError(
                    f"invalid source_names for upstream project {project.upstream_key!r}"
                )
            if not project.source_names:
                raise AllocationError(
                    f"upstream project {project.upstream_key!r} has no source names"
                )
            if len(project.source_names) != len(set(project.source_names)):
                raise AllocationError(
                    f"duplicate source name for upstream project {project.upstream_key!r}"
                )
            for source_name in project.source_names:
                if source_name in source_owner:
                    raise AllocationError(
                        f"source {source_name!r} belongs to multiple upstream projects"
                    )
                source_owner[source_name] = project.upstream_key
                resolution = resolution_by_source.get(source_name)
                if resolution is None:
                    raise AllocationError(
                        f"missing resolution for project source {source_name!r}"
                    )
                if (
                    resolution.method == "none"
                    or resolution.upstream_key != project.upstream_key
                ):
                    raise AllocationError(
                        f"resolution identity does not match project for source {source_name!r}"
                    )

        for source_name, resolution in resolution_by_source.items():
            if resolution.method == "none":
                if source_name in source_owner:
                    raise AllocationError(
                        f"no-upstream source {source_name!r} is included in a project"
                    )
                continue
            matching_project = project_by_key.get(resolution.upstream_key)
            if matching_project is None or source_name not in matching_project.source_names:
                raise AllocationError(
                    f"resolved source {source_name!r} has no matching upstream project"
                )

        if not projects:
            raise AllocationError("session has no eligible upstream projects")

        project_keys = [project.upstream_key for project in projects]
        maintainer_rows = db.execute(
            select(Maintainer.upstream_key, Maintainer.opted_in).where(
                Maintainer.upstream_key.in_(project_keys)
            )
        ).all()
        opted_in_by_key: dict[str, bool] = {}
        for upstream_key, opted_in in maintainer_rows:
            if upstream_key in opted_in_by_key:
                raise AllocationError(f"duplicate maintainer for {upstream_key!r}")
            if type(opted_in) is not bool:
                raise AllocationError(f"invalid maintainer opt-in for {upstream_key!r}")
            opted_in_by_key[upstream_key] = opted_in

        assembled: list[ProjectInput] = []
        for project in sorted(projects, key=lambda item: item.upstream_key):
            matching_resolutions = [
                resolution_by_source[name] for name in project.source_names
            ]
            try:
                snapshot: FactsSnapshot = build_facts_snapshot(
                    project,
                    matching_resolutions,
                    expected_binary_count=project.binary_count,
                )
                normalized = snapshot.normalized
                facts = ProjectFacts(
                    archived=normalized.archived,
                    last_commit=normalized.last_commit,
                    committers_12m=normalized.committers_12m,
                    partial=normalized.partial,
                    funding_yml=normalized.funding_yml,
                    snapshot=snapshot,
                )
                assembled.append(ProjectInput(
                    upstream_key=project.upstream_key,
                    display_name=project.display_name,
                    binary_count=project.binary_count,
                    facts=facts,
                    opted_in=opted_in_by_key.get(project.upstream_key, False),
                ))
            except (TypeError, ValueError) as exc:
                raise AllocationError(
                    f"invalid persisted allocation facts for {project.upstream_key!r}: {exc}"
                ) from exc

        return AllocationInputSet(tuple(assembled), evaluation_date)

    @staticmethod
    def create(
        db: DbSession,
        session_id: str,
        budget_cents: int,
        evaluation_date: date,
    ) -> Plan:
        """Create a draft plan and its lines/event inside one caller-owned transaction.

        A nested transaction/savepoint makes this operation atomic while leaving
        commit ownership with the caller. Existing caller work is never committed.
        """
        if type(budget_cents) is not int or not 100 <= budget_cents <= 1_000_000:
            raise ValidationError(
                "budget_cents must be an integer between 100 and 1000000",
                code="invalid_budget",
            )

        inputs = PlanService.assemble_allocation_inputs(db, session_id, evaluation_date)
        if not inputs.projects:
            raise AllocationError("session has no eligible upstream projects")
        policy = load_allocation_policy()
        result = allocate(list(inputs.projects), budget_cents, policy, inputs.evaluation_date)

        # The allocator emits canonical upstream-key order. Keep that ordering
        # explicit at persistence even if its result implementation changes.
        lines = sorted(result.lines, key=lambda line: line.upstream_key)
        plan = Plan(
            session_id=session_id,
            state="draft",
            budget_cents=budget_cents,
            payable_total_cents=result.payable_total_cents,
            reserved_total_cents=result.reserved_total_cents,
            est_payout_fees_cents=result.est_payout_fees_cents,
            # No charge formula is accepted while the required fee estimates
            # are unavailable; keep the estimate unknown.
            charge_cents=None,
        )

        connection = db.connection()
        if (
            connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction
        ):
            # sqlite3's legacy mode does not BEGIN for SELECT; releasing a
            # savepoint without a physical outer transaction would commit it.
            connection.exec_driver_sql("BEGIN")
        with db.begin_nested():
            db.add(plan)
            db.flush()
            for line in lines:
                db.add(PlanLine(
                    plan_id=plan.id,
                    upstream_key=line.upstream_key,
                    display_name=line.display_name,
                    weight=line.weight,
                    amount_cents=line.amount_cents,
                    payable_cents=line.payable_cents,
                    reserved_cents=line.reserved_cents,
                    reason=line.reason,
                    facts_snapshot=line.facts_snapshot.to_dict(),
                ))
            db.flush()
            append_ledger_event(
                db,
                "PLAN_CREATED",
                {
                    "budget_cents": budget_cents,
                    "payable_total_cents": result.payable_total_cents,
                    "reserved_total_cents": result.reserved_total_cents,
                    "line_count": len(lines),
                },
                plan_id=plan.id,
                session_id=session_id,
            )
            db.flush()
        return plan

    @staticmethod
    def adjust_budget(
        db: DbSession,
        plan_id: str,
        budget_cents: int,
        evaluation_date: date,
    ) -> Plan:
        """Recalculate a draft plan atomically, leaving commit ownership to caller."""
        if type(plan_id) is not str or not plan_id:
            raise NotFoundError("Plan not found", code="plan_not_found")
        plan = db.get(Plan, plan_id)
        if plan is None:
            raise NotFoundError("Plan not found", code="plan_not_found")
        if plan.state != "draft" or plan.frozen_at is not None:
            raise StateError(
                "Only unfrozen draft plans can have their budget adjusted",
                code="invalid_plan_transition",
                current_state=plan.state,
                allowed_transitions=["draft"] if plan.state != "draft" else [],
            )
        if type(budget_cents) is not int or not 100 <= budget_cents <= 1_000_000:
            raise ValidationError(
                "budget_cents must be an integer between 100 and 1000000",
                code="invalid_budget",
            )

        old_budget_cents = plan.budget_cents
        inputs = PlanService.assemble_allocation_inputs(
            db, plan.session_id, evaluation_date
        )
        if not inputs.projects:
            raise AllocationError("session has no eligible upstream projects")
        policy = load_allocation_policy()
        result = allocate(list(inputs.projects), budget_cents, policy, inputs.evaluation_date)
        lines = sorted(result.lines, key=lambda line: line.upstream_key)

        connection = db.connection()
        if (
            connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction
        ):
            connection.exec_driver_sql("BEGIN")
        with db.begin_nested():
            guarded_update = db.execute(
                update(Plan)
                .where(
                    Plan.id == plan_id,
                    Plan.state == "draft",
                    Plan.frozen_at.is_(None),
                )
                .values(
                    budget_cents=budget_cents,
                    payable_total_cents=result.payable_total_cents,
                    reserved_total_cents=result.reserved_total_cents,
                    est_payout_fees_cents=result.est_payout_fees_cents,
                    charge_cents=None,
                )
                .execution_options(synchronize_session="fetch")
            )
            if guarded_update.rowcount != 1:
                current = db.execute(
                    select(Plan.state).where(Plan.id == plan_id)
                ).one_or_none()
                if current is None:
                    raise NotFoundError("Plan not found", code="plan_not_found")
                current_state = current[0]
                raise StateError(
                    "Only unfrozen draft plans can have their budget adjusted",
                    code="invalid_plan_transition",
                    current_state=current_state,
                    allowed_transitions=(
                        ["draft"]
                        if current_state != "draft"
                        else []
                    ),
                )

            db.execute(delete(PlanLine).where(PlanLine.plan_id == plan_id))
            for line in lines:
                db.add(PlanLine(
                    plan_id=plan_id,
                    upstream_key=line.upstream_key,
                    display_name=line.display_name,
                    weight=line.weight,
                    amount_cents=line.amount_cents,
                    payable_cents=line.payable_cents,
                    reserved_cents=line.reserved_cents,
                    reason=line.reason,
                    facts_snapshot=line.facts_snapshot.to_dict(),
                ))
            db.flush()
            append_ledger_event(
                db,
                "PLAN_BUDGET_ADJUSTED",
                {
                    "old_budget_cents": old_budget_cents,
                    "new_budget_cents": budget_cents,
                },
                plan_id=plan_id,
                session_id=plan.session_id,
            )
            db.flush()
            db.expire(plan, ["lines"])
        return plan

    @staticmethod
    def get(db: DbSession, plan_id: str) -> Plan:
        plan = db.get(Plan, plan_id) if type(plan_id) is str and plan_id else None
        if plan is None:
            raise NotFoundError("Plan not found", code="plan_not_found")
        return plan

    @staticmethod
    def approve(db: DbSession, plan_id: str) -> Plan:
        """Freeze a draft through LedgerService's guarded transition.

        The transition helper owns and commits its supplied session per the
        LLD. Use a clean, dedicated session so caller-owned work is untouched.
        """
        plan = PlanService.get(db, plan_id)
        if plan.state == "funding" and plan.frozen_at is not None and plan.order_attempt == 1:
            return plan
        if plan.state != "draft" or plan.frozen_at is not None:
            raise StateError(
                "Only an unfrozen draft plan can be approved",
                code="invalid_plan_transition",
                current_state=plan.state,
                allowed_transitions=["funding"] if plan.state == "draft" else [],
            )
        if plan in db.dirty:
            raise ValidationError(
                "Cannot approve a plan with caller-owned pending edits",
                code="pending_plan_edits",
            )

        transition_db = DbSession(
            bind=db.get_bind(), autoflush=False, expire_on_commit=False
        )
        try:
            current = PlanService.get(transition_db, plan_id)
            if current.state == "funding" and current.frozen_at is not None and current.order_attempt == 1:
                pass  # Another identical request already committed approval.
            elif current.state != "draft" or current.frozen_at is not None:
                raise StateError(
                    "Only an unfrozen draft plan can be approved",
                    code="invalid_plan_transition",
                    current_state=current.state,
                    allowed_transitions=["funding"] if current.state == "draft" else [],
                )
            else:
                current.frozen_at = datetime.now(UTC)
                current.order_attempt = 1
                LedgerService.transition_plan(
                    transition_db,
                    plan_id,
                    "draft",
                    "funding",
                    "PLAN_APPROVED",
                    {
                        "charge_cents": current.charge_cents,
                        "order_attempt": 1,
                    },
                )
        finally:
            transition_db.close()

        db.expire(plan)
        return plan
