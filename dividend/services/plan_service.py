"""Plan-service preparation of immutable allocation inputs."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hmac
import re

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from ..allocation import (
    AllocationError, FactsSnapshot, ProjectFacts, ProjectInput, allocate,
    load_allocation_policy,
)
from ..allocation.snapshot import build_facts_snapshot
from ..allocation.maintainer import SERVER_EVIDENCE_PROVENANCE, distribute
from ..errors import CaptureMismatch, NotFoundError, StateError, ValidationError
from ..ledger import LedgerService, append as append_ledger_event
from ..models import (LedgerEvent, Maintainer, MaintainerRecipient, Plan,
                      PlanLine, Session, SourceResolution, UpstreamProject)
from .approval_proposal import freeze_proposal, proposal_sha256
from .maintainer_evidence import load_demo_identity_map, load_evidence_snapshot_context
from ..payments.fees import (ROUNDING_POLICY, calculate_gross_charge_cents,
                             estimate_payout_fee_cents)

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_REPOSITORY_KEY = "github.com/curl/curl"


@dataclass(frozen=True, slots=True)
class AllocationInputSet:
    """Canonical domain inputs and the caller's evaluation date."""

    projects: tuple[ProjectInput, ...]
    evaluation_date: date
    recipients_by_project: dict[str, tuple["MaintainerShare", ...]] | None = None
    evidence_by_project: dict[str, tuple[dict, ...]] | None = None
    evidence_context: dict | None = None


@dataclass(frozen=True, slots=True)
class MaintainerShare:
    maintainer_id: str
    display_name: str
    email: str
    share_bps: int


def split_cents_by_share(amount_cents: int, recipients: tuple[MaintainerShare, ...]) -> list[dict]:
    """Split cents by basis points using largest remainder and stable ID ties."""
    if type(amount_cents) is not int or amount_cents < 0:
        raise AllocationError("recipient split amount must be nonnegative integer cents")
    if amount_cents == 0:
        return []
    if not recipients:
        return []
    if sum(item.share_bps for item in recipients) != 10000:
        raise AllocationError("eligible maintainer shares must total exactly 100 percent")
    if amount_cents < len(recipients):
        raise AllocationError("payable allocation is too small to pay every eligible maintainer")
    floors, remainders = {}, {}
    for item in recipients:
        floors[item.maintainer_id], remainders[item.maintainer_id] = divmod(
            amount_cents * item.share_bps, 10000,
        )
    remaining = amount_cents - sum(floors.values())
    for maintainer_id in sorted(remainders, key=lambda key: (-remainders[key], key))[:remaining]:
        floors[maintainer_id] += 1
    if any(floors[item.maintainer_id] <= 0 for item in recipients):
        raise AllocationError("payable allocation cannot fund every eligible maintainer share")
    return [{
        "maintainer_id": item.maintainer_id,
        "display_name": item.display_name,
        "share_bps": item.share_bps,
        "amount_cents": floors[item.maintainer_id],
    } for item in sorted(recipients, key=lambda value: value.maintainer_id)]


class PlanService:
    """Application boundary for preparing inputs to the pure allocator."""

    @staticmethod
    def _score_maintainers(lines, inputs: AllocationInputSet) -> dict[str, dict]:
        scored = {}
        for line in lines:
            recipients = [{"id": row.maintainer_id, "display_name": row.display_name,
                           "opted_in": True}
                          for row in (inputs.recipients_by_project or {}).get(line.upstream_key, ())]
            result = distribute(
                line.payable_cents, recipients,
                list((inputs.evidence_by_project or {}).get(line.upstream_key, ())),
            )
            splits = [{key: row[key] for key in (
                "maintainer_id", "display_name", "share_bps", "amount_cents",
            )} for row in result["maintainers"] if row["amount_cents"] > 0]
            payable = sum(row["amount_cents"] for row in splits)
            reserved = line.reserved_cents + result["reserved_cents"]
            if payable + reserved != line.amount_cents:
                raise AllocationError("maintainer evidence allocation does not preserve project cents")
            scored[line.upstream_key] = {
                "payable_cents": payable, "reserved_cents": reserved,
                "maintainers": splits,
            }
        return scored

    @staticmethod
    def _approval_proposal(lines, allocations: dict[str, dict], inputs: AllocationInputSet,
                           policy_version: str) -> dict:
        context = inputs.evidence_context or {}
        projects = []
        for line in sorted(lines, key=lambda row: row.upstream_key):
            evidence = (inputs.evidence_by_project or {}).get(line.upstream_key, ())
            projects.append({
                "project_id": line.upstream_key,
                "amount_cents": line.amount_cents,
                "payable_cents": allocations[line.upstream_key]["payable_cents"],
                "maintainers": [{
                    "maintainer_id": row["maintainer_id"],
                    "amount_cents": row["amount_cents"],
                    "share_bps": row["share_bps"],
                } for row in allocations[line.upstream_key]["maintainers"]],
                "reserved_cents": allocations[line.upstream_key]["reserved_cents"],
                "evidence": [{
                    "event_id": row["event_id"], "github_user_id": row["github_user_id"],
                    "signal_type": row["signal_type"], "pr_id": row["pr_id"],
                    "pr_number": row["pr_number"], "source_url": row["reference"],
                    "timestamp": row["timestamp"],
                    "mapped_maintainer_id": row["maintainer_id"],
                } for row in evidence],
            })
        return {
            "schema_version": 1,
            "projects": projects,
            "evidence_snapshot": {
                "snapshot_id": context.get("snapshot_id"),
                "captured_at": context.get("captured_at"),
            },
            "identity_mapping": {
                "mapping_id": context.get("identity_mapping_id"),
                "seeded_demo": context.get("seeded_demo") is True,
            },
            "allocation_policy_version": policy_version,
            "currency": "USD",
        }

    @staticmethod
    def verify_approval_proposal(db: DbSession, plan: Plan) -> str:
        snapshot = plan.fee_policy_snapshot
        frozen = snapshot.get("approval_proposal") if type(snapshot) is dict else None
        proposal = frozen.get("snapshot") if type(frozen) is dict else None
        stored_hash = frozen.get("sha256") if type(frozen) is dict else None
        if (type(proposal) is not dict or type(stored_hash) is not str
                or type(proposal.get("projects")) is not list
                or type(proposal.get("evidence_snapshot")) is not dict
                or type(proposal.get("identity_mapping")) is not dict
                or proposal.get("schema_version") != 1
                or proposal.get("currency") != "USD"
                or proposal.get("allocation_policy_version") != plan.fee_policy_version):
            raise ValidationError("Approval proposal snapshot is missing or has failed integrity verification",
                                  code="approval_snapshot_invalid")
        try:
            valid_hash = hmac.compare_digest(proposal_sha256(proposal), stored_hash)
        except (TypeError, ValueError):
            valid_hash = False
        if not valid_hash:
            raise ValidationError("Approval proposal snapshot is missing or has failed integrity verification",
                                  code="approval_snapshot_invalid")
        if (proposal.get("synthetic_demo", False) is not plan.synthetic_demo
                or (plan.synthetic_demo and (
                    proposal.get("synthetic_fixture_version") != plan.synthetic_fixture_version
                    or proposal.get("synthetic_policy_version") != plan.synthetic_policy_version
                ))):
            raise ValidationError("Proposal synthetic classification does not match the plan",
                                  code="approval_snapshot_invalid")
        expected = [{
            "project_id": line.upstream_key,
            "amount_cents": line.amount_cents,
            "payable_cents": line.payable_cents,
            "reserved_cents": line.reserved_cents,
            "maintainers": sorted([
                {"maintainer_id": row.get("maintainer_id"),
                 "amount_cents": row.get("amount_cents"), "share_bps": row.get("share_bps")}
                for row in (line.maintainer_splits or []) if type(row) is dict
            ], key=lambda row: row["maintainer_id"] or ""),
        } for line in sorted(plan.lines, key=lambda row: row.upstream_key)]
        actual = [{
            "project_id": row.get("project_id"),
            "amount_cents": row.get("amount_cents"),
            "payable_cents": row.get("payable_cents"),
            "reserved_cents": row.get("reserved_cents"),
            "maintainers": sorted(row.get("maintainers", []), key=lambda item: item.get("maintainer_id", "")),
        } for row in proposal.get("projects", []) if type(row) is dict]
        if (actual != expected
                or any(row["payable_cents"] != sum(item["amount_cents"] for item in row["maintainers"])
                       for row in expected)
                or sum(row["amount_cents"] for row in expected) != plan.budget_cents
                or sum(row["payable_cents"] for row in expected) != plan.payable_total_cents
                or sum(row["reserved_cents"] for row in expected) != plan.reserved_total_cents):
            raise ValidationError("Approval proposal does not match the persisted plan lines",
                                  code="approval_snapshot_invalid")
        if plan.frozen_at is not None:
            event = db.scalar(select(LedgerEvent).where(
                LedgerEvent.plan_id == plan.id, LedgerEvent.type == "PLAN_APPROVED",
            ).order_by(LedgerEvent.created_at.desc()).limit(1))
            if event is None or event.payload.get("approval_snapshot_sha256") != stored_hash:
                raise ValidationError("Approved proposal hash does not match its ledger record",
                                      code="approval_snapshot_invalid")
        return stored_hash

    @staticmethod
    def assemble_allocation_inputs(
        db: DbSession,
        session_id: str,
        evaluation_date: date,
        *,
        include_maintainer_evidence: bool = True,
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
                "override", "repo_url", "official_source", "llm", "none"
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

        # A candidate mapping is useful in discovery, but only validator-approved
        # identities may influence a funding decision.
        projects = [project for project in projects if project.source_names and all(
            resolution_by_source.get(name) is not None
            and resolution_by_source[name].upstream_key == project.upstream_key
            and resolution_by_source[name].validated is True
            for name in project.source_names
        )]

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
            if resolution.method == "none" or resolution.validated is not True:
                if source_name in source_owner:
                    raise AllocationError(
                        f"ineligible source {source_name!r} is included in a project"
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
        if not include_maintainer_evidence:
            recipients_by_key = {}
            opted_in_by_key = {}
            evidence_by_key = {}
            evidence_context = {}
        else:
            legacy_rows = list(db.scalars(select(Maintainer).where(
                Maintainer.upstream_key.in_(project_keys)
            )))
            additional_rows = list(db.scalars(select(MaintainerRecipient).where(
                MaintainerRecipient.upstream_key.in_(project_keys)
            )))
            legacy_by_key = {row.upstream_key: row for row in legacy_rows}
            additional_by_key: dict[str, list[MaintainerRecipient]] = {}
            for row in additional_rows:
                additional_by_key.setdefault(row.upstream_key, []).append(row)

            recipients_by_key: dict[str, tuple[MaintainerShare, ...]] = {}
            opted_in_by_key: dict[str, bool] = {}
            for key in project_keys:
                rows = additional_by_key.get(key)
                candidates = rows if rows is not None else ([legacy_by_key[key]] if key in legacy_by_key else [])
                configured_eligible = []
                for row in candidates:
                    if type(row.opted_in) is not bool:
                        raise AllocationError(f"invalid maintainer opt-in for {key!r}")
                    if row.opted_in is True and row.active is True and row.market_code == "US":
                        if type(row.email) is not str or not _EMAIL.fullmatch(row.email):
                            raise AllocationError(f"eligible maintainer has invalid recipient details for {key!r}")
                        if type(row.share_bps) is not int or not 1 <= row.share_bps <= 10000:
                            raise AllocationError(f"invalid maintainer share for {key!r}")
                        configured_eligible.append(MaintainerShare(
                            row.id, row.display_name, row.email, row.share_bps,
                        ))
                if configured_eligible and sum(row.share_bps for row in configured_eligible) != 10000:
                    raise AllocationError(f"eligible maintainer shares for {key!r} must total exactly 100 percent")
                recipients_by_key[key] = tuple(configured_eligible)
                opted_in_by_key[key] = bool(configured_eligible)

            evidence_context = load_evidence_snapshot_context()
            evidence_by_key: dict[str, tuple[dict, ...]] = {}
            if _REPOSITORY_KEY in project_keys:
                identity_map = load_demo_identity_map()
                legacy_id_by_key = {row.upstream_key: row.id for row in legacy_rows}
                for evidence in evidence_context["evidence"]:
                    upstream_key = identity_map.get(evidence["github_user_id"])
                    maintainer_id = legacy_id_by_key.get(upstream_key)
                    evidence_by_key.setdefault(_REPOSITORY_KEY, ())
                    evidence_by_key[_REPOSITORY_KEY] += ({
                        **evidence,
                        "maintainer_id": maintainer_id,
                        "provenance": SERVER_EVIDENCE_PROVENANCE,
                    },)

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

        return AllocationInputSet(tuple(assembled), evaluation_date, recipients_by_key,
                                  evidence_by_key, evidence_context)

    @staticmethod
    def create(
        db: DbSession,
        session_id: str,
        budget_cents: int,
        evaluation_date: date,
        policy_path=None,
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

        session = db.get(Session, session_id)
        if session is not None and session.synthetic_demo:
            raise ValidationError("Synthetic demo sessions require the dedicated demo service",
                                  code="synthetic_demo_isolated")
        inputs = PlanService.assemble_allocation_inputs(db, session_id, evaluation_date)
        if not inputs.projects:
            raise AllocationError("session has no eligible upstream projects")
        policy = load_allocation_policy(policy_path) if policy_path is not None else load_allocation_policy()
        result = allocate(list(inputs.projects), budget_cents, policy, inputs.evaluation_date)
        line_allocations = PlanService._score_maintainers(result.lines, inputs)
        payable_total_cents = sum(row["payable_cents"] for row in line_allocations.values())
        reserved_total_cents = sum(row["reserved_cents"] for row in line_allocations.values())
        estimates = PlanService._fee_calculation(
            payable_total_cents,
            sum(len(row["maintainers"]) for row in line_allocations.values()),
            policy.fees,
        )
        proposal = PlanService._approval_proposal(
            result.lines, line_allocations, inputs, policy.fees.version,
        )
        fee_snapshot = {**estimates, "approval_proposal": freeze_proposal(proposal)}

        # The allocator emits canonical upstream-key order. Keep that ordering
        # explicit at persistence even if its result implementation changes.
        lines = sorted(result.lines, key=lambda line: line.upstream_key)
        plan = Plan(
            session_id=session_id,
            state="draft",
            budget_cents=budget_cents,
            payable_total_cents=payable_total_cents,
            reserved_total_cents=reserved_total_cents,
            est_payout_fees_cents=estimates["estimated_payout_fee_cents"],
            est_incoming_fee_cents=estimates["estimated_incoming_fee_cents"],
            charge_cents=estimates["gross_charge_cents"],
            fee_policy_version=policy.fees.version,
            fee_policy_snapshot=fee_snapshot,
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
                    payable_cents=line_allocations[line.upstream_key]["payable_cents"],
                    reserved_cents=line_allocations[line.upstream_key]["reserved_cents"],
                    reason=line.reason,
                    facts_snapshot=line.facts_snapshot.to_dict(),
                    maintainer_splits=line_allocations[line.upstream_key]["maintainers"],
                ))
            db.flush()
            append_ledger_event(
                db,
                "PLAN_CREATED",
                {
                    "budget_cents": budget_cents,
                    "payable_total_cents": payable_total_cents,
                    "reserved_total_cents": reserved_total_cents,
                    "line_count": len(lines),
                },
                plan_id=plan.id,
                session_id=session_id,
            )
            append_ledger_event(
                db, "FEE_ESTIMATED", estimates, plan_id=plan.id, session_id=session_id
            )
            db.flush()
        return plan

    @staticmethod
    def adjust_budget(
        db: DbSession,
        plan_id: str,
        budget_cents: int,
        evaluation_date: date,
        policy_path=None,
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
        if db.get(Session, plan.session_id).synthetic_demo:
            raise ValidationError("Synthetic demo plans cannot use normal budget adjustment",
                                  code="synthetic_demo_isolated")
        inputs = PlanService.assemble_allocation_inputs(
            db, plan.session_id, evaluation_date
        )
        if not inputs.projects:
            raise AllocationError("session has no eligible upstream projects")
        policy = load_allocation_policy(policy_path) if policy_path is not None else load_allocation_policy()
        result = allocate(list(inputs.projects), budget_cents, policy, inputs.evaluation_date)
        line_allocations = PlanService._score_maintainers(result.lines, inputs)
        payable_total_cents = sum(row["payable_cents"] for row in line_allocations.values())
        reserved_total_cents = sum(row["reserved_cents"] for row in line_allocations.values())
        estimates = PlanService._fee_calculation(
            payable_total_cents,
            sum(len(row["maintainers"]) for row in line_allocations.values()),
            policy.fees,
        )
        proposal = PlanService._approval_proposal(
            result.lines, line_allocations, inputs, policy.fees.version,
        )
        fee_snapshot = {**estimates, "approval_proposal": freeze_proposal(proposal)}
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
                    payable_total_cents=payable_total_cents,
                    reserved_total_cents=reserved_total_cents,
                    est_payout_fees_cents=estimates["estimated_payout_fee_cents"],
                    est_incoming_fee_cents=estimates["estimated_incoming_fee_cents"],
                    charge_cents=estimates["gross_charge_cents"],
                    fee_policy_version=policy.fees.version,
                    fee_policy_snapshot=fee_snapshot,
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
                    payable_cents=line_allocations[line.upstream_key]["payable_cents"],
                    reserved_cents=line_allocations[line.upstream_key]["reserved_cents"],
                    reason=line.reason,
                    facts_snapshot=line.facts_snapshot.to_dict(),
                    maintainer_splits=line_allocations[line.upstream_key]["maintainers"],
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
            append_ledger_event(db, "FEE_ESTIMATED", estimates, plan_id=plan_id,
                                session_id=plan.session_id)
            db.flush()
            db.expire(plan, ["lines"])
        return plan

    @staticmethod
    def _fee_calculation(target_distribution_cents: int, item_count: int, fees) -> dict:
        supported = fees.measured and fees.supports_standard_us_usd
        payout_fee = estimate_payout_fee_cents(item_count) if supported else None
        charge = incoming = None
        if supported and target_distribution_cents > 0:
            charge, incoming = calculate_gross_charge_cents(
                target_distribution_cents, payout_fee, fees.buffer_cents or 0
            )
        return {
            "version": fees.version,
            "scope": {
                "merchant_market": fees.merchant_market,
                "payer_market_assumption": fees.payer_market_assumption,
                "recipient_market": fees.recipient_market,
                "currency": fees.currency,
                "checkout_method": fees.checkout_method,
                "payout_method": fees.payout_method,
                "pricing_tier": fees.pricing_tier,
                "currency_conversion": fees.currency_conversion,
                "micropayments": fees.micropayments,
                "charity_pricing": fees.charity_pricing,
                "negotiated_rates": fees.negotiated_rates,
            },
            "assumptions": {
                "incoming_percentage": str(fees.order_fee_pct) if supported else None,
                "incoming_fixed_cents": fees.order_fee_fixed_cents if supported else None,
                "payout_fee_per_item_cents": fees.payout_fee_per_item_cents if supported else None,
                "buffer_cents": fees.buffer_cents if supported else None,
                "tolerance_cents": fees.tolerance_cents,
                "rounding_policy": ROUNDING_POLICY if supported else None,
            },
            "target_distribution_cents": target_distribution_cents,
            "payout_item_count": item_count,
            "estimated_payout_fee_cents": payout_fee,
            "estimated_incoming_fee_cents": incoming,
            "gross_charge_cents": charge,
            "supported": supported,
        }

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
        PlanService.verify_approval_proposal(db, plan)
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
            approval_hash = PlanService.verify_approval_proposal(transition_db, current)
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
                        "approval_snapshot_sha256": approval_hash,
                    },
                )
        finally:
            transition_db.close()

        db.expire(plan)
        return plan

    @staticmethod
    def transition(db: DbSession, plan_id: str, from_state: str, to_state: str,
                   event_type: str, payload: dict, *, additional_events=None) -> None:
        """Keep all production plan-state writes behind PlanService."""
        LedgerService.transition_plan(db, plan_id, from_state, to_state, event_type,
                                      payload, additional_events=additional_events)

    @staticmethod
    def mark_captured(db: DbSession, plan_id: str, capture,
                      tolerance_cents: int | None = None, *,
                      payer_market_code: str | None = None) -> Plan:
        plan = PlanService.get(db, plan_id)
        if plan.state != "funding":
            raise StateError("Plan is not awaiting capture", code="invalid_plan_transition",
                             current_state=plan.state, allowed_transitions=["funded"])
        if plan.charge_cents is None or plan.est_payout_fees_cents is None:
            raise ValidationError("Funding requirements are unavailable", code="charge_unavailable")
        amounts = (capture.gross_cents, capture.fee_cents, capture.net_cents)
        if (type(capture.gross_cents) is not int or capture.gross_cents < 0
                or (capture.fee_cents is not None and
                    (type(capture.fee_cents) is not int or capture.fee_cents < 0))
                or type(capture.net_cents) is not int or capture.net_cents < 0):
            raise CaptureMismatch("Capture amounts must be nonnegative integer cents", code="capture_mismatch")
        if (type(capture.capture_id) is not str or not capture.capture_id
                or type(capture.currency) is not str or type(capture.status) is not str
                or capture.currency != "USD" or capture.gross_cents != plan.charge_cents
                or (capture.fee_cents is not None and
                    capture.gross_cents - capture.fee_cents != capture.net_cents)
                or capture.status.upper() != "COMPLETED"):
            reason = "CAPTURE_MISMATCH"
            plan.failure_reason = reason
            plan.capture_gross_cents, plan.capture_fee_cents, plan.capture_net_cents = amounts
            PlanService.transition(db, plan_id, "funding", "failed", reason, {
                "capture_id": getattr(capture, "capture_id", None),
                "gross_cents": capture.gross_cents, "charge_cents": plan.charge_cents,
                "fee_cents": capture.fee_cents, "net_cents": capture.net_cents,
                "currency": capture.currency, "status": capture.status,
            })
            return plan

        if not isinstance(plan.fee_policy_snapshot, dict):
            raise ValidationError("Plan has no frozen fee policy snapshot", code="fee_policy_unavailable")
        tolerance = plan.fee_policy_snapshot.get("assumptions", {}).get("tolerance_cents")
        if type(tolerance) is not int or tolerance < 0:
            raise ValidationError("Plan fee policy snapshot has invalid tolerance", code="fee_policy_unavailable")
        if tolerance_cents is not None and tolerance_cents != tolerance:
            raise ValidationError("Current fee policy differs from the frozen plan",
                                  code="fee_policy_version_mismatch")
        if payer_market_code != "US":
            plan.failure_reason = "UNSUPPORTED_MARKET"
            plan.capture_gross_cents, plan.capture_fee_cents, plan.capture_net_cents = amounts
            PlanService.transition(db, plan_id, "funding", "failed",
                                   "UNSUPPORTED_TRANSACTION_SCOPE", {
                "capture_id": capture.capture_id,
                "payer_market_code": payer_market_code,
                "gross_cents": capture.gross_cents,
                "fee_cents": capture.fee_cents,
                "net_cents": capture.net_cents,
            }, additional_events=[{"type": "ORDER_CAPTURED", "payload": {
                "capture_id": capture.capture_id, "gross_cents": capture.gross_cents,
                "fee_cents": capture.fee_cents, "net_cents": capture.net_cents,
            }, "external_id": capture.capture_id}, {"type": "FEE_ACTUALIZED", "payload": {
                "estimated_cents": plan.est_incoming_fee_cents,
                "actual_cents": capture.fee_cents, "net_cents": capture.net_cents,
            }, "external_id": capture.capture_id}])
            return plan
        required = plan.payable_total_cents + plan.est_payout_fees_cents
        plan.capture_gross_cents, plan.capture_fee_cents, plan.capture_net_cents = amounts
        if capture.net_cents < required - tolerance:
            plan.failure_reason = "FUNDING_SHORTFALL"
            PlanService.transition(db, plan_id, "funding", "failed", "FUNDING_SHORTFALL", {
                "required_cents": required, "net_cents": capture.net_cents,
                "tolerance_cents": tolerance,
            }, additional_events=[{"type": "ORDER_CAPTURED", "payload": {
                "capture_id": capture.capture_id, "gross_cents": capture.gross_cents,
                "fee_cents": capture.fee_cents, "net_cents": capture.net_cents,
            }, "external_id": capture.capture_id}, {"type": "FEE_ACTUALIZED", "payload": {
                "estimated_cents": plan.est_incoming_fee_cents,
                "actual_cents": capture.fee_cents, "net_cents": capture.net_cents,
            }, "external_id": capture.capture_id}])
            return plan

        events = [{"type": "FEE_ACTUALIZED", "payload": {
            "estimated_cents": plan.est_incoming_fee_cents,
            "actual_cents": capture.fee_cents, "net_cents": capture.net_cents,
        }, "external_id": capture.capture_id}]
        if capture.net_cents > required + tolerance:
            events.append({"type": "SURPLUS_RETAINED", "payload": {
                "amount_cents": capture.net_cents - required,
            }, "external_id": capture.capture_id})
        PlanService.transition(db, plan_id, "funding", "funded", "ORDER_CAPTURED", {
            "capture_id": capture.capture_id, "gross_cents": capture.gross_cents,
            "fee_cents": capture.fee_cents, "net_cents": capture.net_cents,
        }, additional_events=events)
        return plan
