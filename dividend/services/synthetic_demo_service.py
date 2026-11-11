"""Create an isolated, reproducible synthetic PayPal Sandbox demo plan."""

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..allocation import allocate, load_allocation_policy
from ..allocation.synthetic_attribution import FIXTURE_PATH, POLICY_PATH, attribute, load_fixture
from ..config import DEFAULT_POLICY_PATH
from ..demo import load_sandbox_recipients
from ..ledger import append as append_ledger_event
from ..models import Plan, PlanLine, Session, SyntheticSandboxRecipient
from ..services.plan_service import PlanService
from ..services.session_service import SessionService
from ..enrichment.fixtures import FixtureEnricher
from ..upstream.overrides import load_overrides
from ..errors import ValidationError

PROJECT_KEY = "github.com/curl/curl"
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SyntheticDemoService:
    @staticmethod
    def create(db: DbSession, settings, budget_cents: int = 4000) -> Plan:
        if settings.PAYMENTS_MODE != "paypal" or settings.PAYPAL_ENV != "sandbox":
            raise ValidationError("Synthetic Sandbox demo requires PayPal Sandbox payment mode",
                                  code="synthetic_demo_sandbox_required")
        if type(budget_cents) is not int or not 100 <= budget_cents <= 1_000_000:
            raise ValidationError("budget_cents must be integer cents between 100 and 1000000",
                                  code="invalid_budget")

        fixture, attribution_policy = load_fixture()
        recipient_path = settings.SYNTHETIC_SANDBOX_RECIPIENTS_FILE
        if recipient_path is None or not Path(recipient_path).is_file():
            raise ValidationError("Sandbox recipient configuration is unavailable",
                                  code="sandbox_recipient_config_unavailable")
        try:
            recipients = load_sandbox_recipients(recipient_path)
        except (OSError, ValueError) as exc:
            raise ValidationError("Sandbox recipient configuration is invalid",
                                  code="sandbox_recipient_config_invalid") from exc
        recipient = recipients.get(PROJECT_KEY)
        if (recipient is None or recipient.get("market_code") != "US"
                or type(recipient.get("email")) is not str
                or not EMAIL.fullmatch(recipient["email"])):
            raise ValidationError("Sandbox recipient configuration is invalid for the synthetic demo",
                                  code="sandbox_recipient_config_invalid")
        recipient_sha = hashlib.sha256(Path(recipient_path).read_bytes()).hexdigest()
        fixture_sha = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
        policy_sha = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
        policy_path = settings.allocation_policy_path if settings.allocation_policy_path.exists() else DEFAULT_POLICY_PATH
        allocation_policy = load_allocation_policy(policy_path)
        run_key = hashlib.sha256(json.dumps({
            "fixture": fixture["fixture_version"], "policy": attribution_policy["policy_version"],
            "recipient_config_sha256": recipient_sha, "policy_sha256": policy_sha,
            "budget_cents": budget_cents,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = db.scalar(select(Session).where(Session.synthetic_run_key == run_key))
        if existing is not None:
            plan = db.scalar(select(Plan).where(Plan.session_id == existing.id))
            if plan is not None:
                return plan
            raise ValueError("Synthetic demo session exists without its plan; contact an administrator")

        session = SessionService.create(
            db, "demo", None, synthetic_demo=True,
            synthetic_fixture_version=fixture["fixture_version"],
            synthetic_policy_version=attribution_policy["policy_version"],
            synthetic_run_key=run_key,
        )
        SessionService.resolve_sources(
            db, session.id,
            FixtureEnricher(Path(__file__).resolve().parents[1] / "data" / "demo_enrichment.json"),
            load_overrides(),
        )
        inputs = PlanService.assemble_allocation_inputs(
            db, session.id, datetime.now(UTC).date(), include_maintainer_evidence=False,
        )
        projects = [replace(project, opted_in=(project.upstream_key == PROJECT_KEY))
                    for project in inputs.projects]
        result = allocate(projects, budget_cents, allocation_policy, datetime.now(UTC).date())

        destination_id = str(uuid5(NAMESPACE_URL, f"dividend-synthetic-sandbox:{recipient_sha}:{PROJECT_KEY}"))
        destination = db.get(SyntheticSandboxRecipient, destination_id)
        if destination is None:
            destination = SyntheticSandboxRecipient(
                id=destination_id, upstream_key=PROJECT_KEY,
                display_name="Synthetic Sandbox recipient", email=recipient["email"],
                market_code="US", config_sha256=recipient_sha,
            )
            db.add(destination)
            db.flush()

        allocations = {}
        evidence_by_project = {}
        for line in result.lines:
            if line.upstream_key == PROJECT_KEY:
                splits, evidence = attribute(
                    line.amount_cents, destination.id, destination.display_name,
                    fixture, attribution_policy,
                )
                payable = sum(row["amount_cents"] for row in splits)
                evidence_by_project[line.upstream_key] = evidence
            else:
                splits, payable = [], 0
            allocations[line.upstream_key] = {
                "payable_cents": payable,
                "reserved_cents": line.amount_cents - payable,
                "maintainers": splits,
            }
        payable_total = sum(row["payable_cents"] for row in allocations.values())
        reserved_total = sum(row["reserved_cents"] for row in allocations.values())
        if payable_total <= 0 or payable_total + reserved_total != budget_cents:
            raise ValueError("Synthetic demo allocation failed payable or budget reconciliation")

        estimates = PlanService._fee_calculation(
            payable_total, sum(len(row["maintainers"]) for row in allocations.values()),
            allocation_policy.fees,
        )
        proposal_projects = []
        for line in sorted(result.lines, key=lambda row: row.upstream_key):
            allocation = allocations[line.upstream_key]
            proposal_projects.append({
                "project_id": line.upstream_key,
                "amount_cents": line.amount_cents,
                "payable_cents": allocation["payable_cents"],
                "maintainers": [{key: row[key] for key in (
                    "maintainer_id", "amount_cents", "share_bps",
                )} for row in allocation["maintainers"]],
                "reserved_cents": allocation["reserved_cents"],
                "evidence": evidence_by_project.get(line.upstream_key, []),
            })
        proposal = {
            "schema_version": 1,
            "projects": proposal_projects,
            "evidence_snapshot": {"snapshot_id": fixture_sha, "captured_at": None},
            "identity_mapping": {"mapping_id": fixture_sha, "type": "fictional_synthetic_fixture"},
            "allocation_policy_version": allocation_policy.fees.version,
            "currency": "USD",
            "synthetic_demo": True,
            "synthetic_fixture_version": fixture["fixture_version"],
            "synthetic_policy_version": attribution_policy["policy_version"],
            "recipient_config_sha256": recipient_sha,
            "synthetic_policy_sha256": policy_sha,
        }
        from .approval_proposal import freeze_proposal

        plan = Plan(
            session_id=session.id, state="draft", synthetic_demo=True,
            synthetic_fixture_version=fixture["fixture_version"],
            synthetic_policy_version=attribution_policy["policy_version"],
            budget_cents=budget_cents, payable_total_cents=payable_total,
            reserved_total_cents=reserved_total,
            est_payout_fees_cents=estimates["estimated_payout_fee_cents"],
            est_incoming_fee_cents=estimates["estimated_incoming_fee_cents"],
            charge_cents=estimates["gross_charge_cents"],
            fee_policy_version=allocation_policy.fees.version,
            fee_policy_snapshot={**estimates, "approval_proposal": freeze_proposal(proposal)},
        )
        db.add(plan)
        db.flush()
        for line in result.lines:
            allocation = allocations[line.upstream_key]
            db.add(PlanLine(
                plan_id=plan.id, upstream_key=line.upstream_key,
                display_name=line.display_name, weight=line.weight,
                amount_cents=line.amount_cents,
                payable_cents=allocation["payable_cents"],
                reserved_cents=allocation["reserved_cents"],
                reason=(line.reason + " SYNTHETIC SANDBOX DEMO: fictional contributor evidence; "
                        "no real maintainer eligibility is established."),
                facts_snapshot=line.facts_snapshot.to_dict(),
                maintainer_splits=allocation["maintainers"],
            ))
        db.flush()
        append_ledger_event(db, "SYNTHETIC_DEMO_PLAN_CREATED", {
            "budget_cents": budget_cents, "payable_total_cents": payable_total,
            "reserved_total_cents": reserved_total,
            "fixture_version": fixture["fixture_version"],
            "policy_version": attribution_policy["policy_version"],
            "recipient_config_sha256": recipient_sha,
        }, plan_id=plan.id, session_id=session.id)
        return plan
