from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from dividend.allocation.maintainer import distribute
from dividend.allocation.synthetic_attribution import load_fixture
from dividend.config import DEFAULT_POLICY_PATH, Settings
from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import ValidationError
from dividend.ledger import list as list_ledger
from dividend.models import (LedgerEvent, Maintainer, MaintainerClaim, Plan,
                             Session, SyntheticSandboxRecipient)
from dividend.payments.fake import FakeProvider
from dividend.services.payment_service import PaymentService
from dividend.services.plan_service import PlanService
from dividend.services.synthetic_demo_service import SyntheticDemoService


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'synthetic.db'}")
    initialize_database(engine)
    yield create_session_factory(engine)
    engine.dispose()


def settings(tmp_path, *, payments_mode="paypal", paypal_env="sandbox"):
    recipient_file = tmp_path / "recipients.yaml"
    recipient_file.write_text(
        "schema: 2\nrecipients:\n  github.com/curl/curl:\n"
        "    email: sandbox@example.test\n    market_code: US\n",
        encoding="utf-8",
    )
    return Settings(
        ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE=payments_mode, PAYPAL_ENV=paypal_env,
        PAYPAL_CLIENT_ID="sandbox-id", PAYPAL_CLIENT_SECRET="sandbox-secret",
        LLM_LIVE_ENABLED=False, SYNTHETIC_SANDBOX_RECIPIENTS_FILE=recipient_file,
        allocation_policy_path=DEFAULT_POLICY_PATH,
    )


def test_production_scorer_rejects_synthetic_evidence():
    fixture, _policy = load_fixture()
    maintainers = [{"id": "fictional", "display_name": "Fictional Contributor", "opted_in": True}]
    evidence = [{
        "maintainer_id": "fictional", "signal_type": item["signal_type"],
        "reference": item["reference"], "provenance": "synthetic-demo-v1",
    } for item in fixture["contributors"][0]["evidence"]]
    result = distribute(1000, maintainers, evidence)
    assert result["maintainers"] == []
    assert result["reserved_cents"] == 1000


def test_synthetic_creation_is_positive_reconciled_and_idempotent(factory, tmp_path):
    config = settings(tmp_path)
    with factory.begin() as db:
        plan = SyntheticDemoService.create(db, config)
        plan_id, session_id = plan.id, plan.session_id
        assert plan.synthetic_demo is True
        assert plan.synthetic_fixture_version == "synthetic-curl-contributors-v1"
        assert plan.synthetic_policy_version == "synthetic-attribution-single-sandbox-destination-v1"
        assert plan.payable_total_cents > 0
        assert plan.budget_cents == plan.payable_total_cents + plan.reserved_total_cents
        assert PlanService.verify_approval_proposal(db, plan)
        assert db.scalar(select(func.count()).select_from(Maintainer)) == 0
        assert db.scalar(select(func.count()).select_from(MaintainerClaim)) == 0
        assert db.scalar(select(func.count()).select_from(SyntheticSandboxRecipient)) == 1
        project_line = next(line for line in plan.lines if line.upstream_key == "github.com/curl/curl")
        assert project_line.payable_cents > 0
        assert project_line.payable_cents + project_line.reserved_cents == project_line.amount_cents
        evidence = plan.fee_policy_snapshot["approval_proposal"]["snapshot"]["projects"]
        curl = next(row for row in evidence if row["project_id"] == "github.com/curl/curl")
        assert all(item["reference"].startswith("synthetic://") for item in curl["evidence"])
        assert all(item["verified"] is False for item in curl["evidence"])
        assert PlanService.verify_approval_proposal(db, plan)

    with factory.begin() as db:
        again = SyntheticDemoService.create(db, config)
        assert again.id == plan_id
        assert again.session_id == session_id
        assert db.scalar(select(func.count()).select_from(Session).where(Session.synthetic_demo.is_(True))) == 1
        assert db.scalar(select(func.count()).select_from(Plan).where(Plan.synthetic_demo.is_(True))) == 1
        assert db.scalar(select(func.count()).select_from(LedgerEvent).where(
            LedgerEvent.type == "SYNTHETIC_DEMO_PLAN_CREATED"
        )) == 1


def test_synthetic_classification_and_policy_are_in_proposal_hash(factory, tmp_path):
    with factory.begin() as db:
        plan = SyntheticDemoService.create(db, settings(tmp_path))
        proposal = plan.fee_policy_snapshot["approval_proposal"]["snapshot"]
        stored_hash = plan.fee_policy_snapshot["approval_proposal"]["sha256"]
        from dividend.services.approval_proposal import proposal_sha256
        assert proposal_sha256(proposal) == stored_hash
        changed = {**proposal, "synthetic_policy_version": "changed"}
        assert proposal_sha256(changed) != stored_hash
        plan.synthetic_demo = False
        with pytest.raises(ValidationError, match="synthetic classification"):
            PlanService.verify_approval_proposal(db, plan)


@pytest.mark.parametrize(("payments_mode", "paypal_env"), [
    ("fake", "sandbox"), ("paypal", "production"),
])
def test_payment_service_rejects_synthetic_plans_outside_paypal_sandbox(
    factory, tmp_path, payments_mode, paypal_env,
):
    with factory.begin() as db:
        plan = SyntheticDemoService.create(db, settings(tmp_path))
        plan_id = plan.id
    service = PaymentService(FakeProvider(), "https://example.test",
                             DEFAULT_POLICY_PATH, payments_mode=payments_mode,
                             paypal_env=paypal_env)
    with factory() as db:
        with pytest.raises(ValidationError, match="PayPal Sandbox"):
            service.approve_and_fund(plan_id, db)


def test_synthetic_session_cannot_use_normal_plan_creator(factory, tmp_path):
    with factory.begin() as db:
        plan = SyntheticDemoService.create(db, settings(tmp_path))
        with pytest.raises(ValidationError, match="dedicated demo service"):
            PlanService.create(db, plan.session_id, 4000, datetime.now(UTC).date())


def test_synthetic_ledger_events_are_marked(factory, tmp_path):
    with factory.begin() as db:
        plan = SyntheticDemoService.create(db, settings(tmp_path))
        events, _cursor = list_ledger(db, plan_id=plan.id)
        assert events
        assert all(event.payload.get("synthetic_demo") is True for event in events)
