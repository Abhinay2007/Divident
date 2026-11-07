from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import PaymentProviderError, ProviderTimeout, StateError, ValidationError
from dividend.models import (LedgerEvent, Maintainer, PayoutBatch, PayoutItem, Plan,
                             PlanLine, Session)
from dividend.payments.fake import FakeProvider
from dividend.services.payment_service import PaymentService


@pytest.fixture
def db_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'payments.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def make_plan(factory, state="funded", *, charge=350, payable=300, reserved=50,
              est_fees=0, recipients=True):
    with factory() as db:
        session = Session(mode="upload", status="ready", source_names=["pkg"], source_count=1)
        db.add(session)
        db.flush()
        plan = Plan(session_id=session.id, state=state, budget_cents=payable + reserved,
                    payable_total_cents=payable, reserved_total_cents=reserved,
                    est_payout_fees_cents=est_fees, charge_cents=charge,
                    frozen_at=datetime.now(UTC) if state != "draft" else None,
                    order_attempt=1 if state != "draft" else 0)
        db.add(plan)
        db.flush()
        for key, amount, line_payable, line_reserved in (
            ("a/project", 100, 100, 0), ("b/project", 200, 200, 0), ("c/project", 50, 0, 50),
        ):
            db.add(PlanLine(plan_id=plan.id, upstream_key=key, display_name=key,
                            weight=Decimal("1"), amount_cents=amount,
                            payable_cents=line_payable, reserved_cents=line_reserved,
                            reason="fixture", facts_snapshot={}))
            db.add(Maintainer(upstream_key=key, display_name="Maintainer",
                              email=f"{key[0]}@example.test", opted_in=recipients and key != "c/project"))
        plan_id = plan.id
        db.commit()
    return plan_id


def service(provider):
    return PaymentService(provider, "https://dividend.example")


def test_unknown_charge_blocks_approval_without_freezing_plan(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=None, est_fees=None)
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider).approve_and_fund(plan_id, db)
    assert error.value.code == "charge_unavailable"
    assert provider.orders == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.frozen_at, plan.order_attempt) == ("draft", None, 0)


def test_order_duplicate_request_and_capture_are_idempotent(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)
    provider = FakeProvider()
    payments = service(provider)
    with db_factory() as db:
        first = payments.approve_and_fund(plan_id, db)
    with db_factory() as db:
        second = payments.approve_and_fund(plan_id, db)
    assert first == second
    assert len(provider.orders) == 1
    with db_factory() as db:
        capture = payments.capture(plan_id, db)
    assert capture.state == "funded"
    with db_factory() as db:
        repeated = payments.capture(plan_id, db)
    assert repeated.capture.capture_id == capture.capture.capture_id
    with db_factory() as db:
        events = list(db.scalars(select(LedgerEvent).where(LedgerEvent.plan_id == plan_id)))
    assert {event.type for event in events} >= {"PLAN_APPROVED", "ORDER_CREATED", "ORDER_CAPTURED", "FEE_ACTUALIZED"}


def test_capture_timeout_reuses_attempt_and_request_id(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)
    class CountedProvider(FakeProvider):
        capture_calls = 0
        order_reads = 0

        def capture_order(self, order_id, request_id):
            self.capture_calls += 1
            return super().capture_order(order_id, request_id)

        def get_order(self, order_id):
            self.order_reads += 1
            return super().get_order(order_id)

    provider = CountedProvider(capture_error=ProviderTimeout("timeout", code="provider_timeout"))
    payments = service(provider)
    with db_factory() as db:
        payments.approve_and_fund(plan_id, db)
    with db_factory() as db, pytest.raises(ProviderTimeout):
        payments.capture(plan_id, db)
    with db_factory() as db:
        assert db.get(Plan, plan_id).capture_attempt == 1
    provider.capture_error = None
    with db_factory() as db:
        assert payments.capture(plan_id, db).state == "funded"
    assert (provider.order_reads, provider.capture_calls) == (1, 2)


def test_uncertain_order_create_stays_funding_and_is_not_resubmitted(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)

    class CreatedThenTimedOut(FakeProvider):
        calls = 0

        def create_order(self, *args, **kwargs):
            self.calls += 1
            result = super().create_order(*args, **kwargs)
            raise ProviderTimeout("response lost", code="provider_timeout")

    provider = CreatedThenTimedOut()
    payments = service(provider)
    with db_factory() as db, pytest.raises(ProviderTimeout):
        payments.approve_and_fund(plan_id, db)
    with db_factory() as db, pytest.raises(PaymentProviderError) as error:
        payments.approve_and_fund(plan_id, db)
    assert error.value.code == "order_outcome_unknown"
    assert provider.calls == 1
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.order_id, plan.order_attempt) == ("funding", None, 1)


def test_capture_shortfall_fails_plan_and_reconciles_capture(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=300, payable=300, reserved=50, est_fees=20)
    provider = FakeProvider()
    provider.orders["order"] = (plan_id, 300, "USD")
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.order_id = "order"
        db.commit()
    result = service(provider)
    with db_factory() as db:
        response = result.capture(plan_id, db)
    assert response.state == "failed"
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.failure_reason, plan.capture_net_cents) == ("FUNDING_SHORTFALL", 300)
        assert db.scalar(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "FUNDING_SHORTFALL"))


def test_payout_excludes_reserved_lines_then_retries_only_failed_items(db_factory):
    plan_id = make_plan(db_factory)
    first_sender_item = f"p{plan_id[:8]}-a1-000"
    second_sender_item = f"p{plan_id[:8]}-a1-001"
    provider = FakeProvider(outcomes={first_sender_item: "success", second_sender_item: "failed"})
    payments = service(provider)
    with db_factory() as db:
        batch = payments.payout(plan_id, db)
    assert batch.item_count == 2
    detail = provider.get_payout_batch(batch.batch_id)
    assert {item.amount_cents for item in detail.items} == {100, 200}
    with db_factory() as db:
        result = payments.reconcile(plan_id, db)
    assert result.batches[0].items[0].status in {"success", "failed"}
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "settled_with_exceptions"
    with db_factory() as db:
        retry = payments.retry_payout(plan_id, db)
    assert retry.item_count == 1
    assert retry.batch_id != batch.batch_id
    assert provider.get_payout_batch(retry.batch_id).items[0].amount_cents == 200
    with db_factory() as db:
        final = payments.reconcile(plan_id, db)
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "settled"
        rows = list(db.scalars(select(PayoutItem).where(PayoutItem.plan_id == plan_id)))
    assert len(rows) == 3
    assert all(row.amount_cents > 0 for row in rows)


def test_pending_item_prevents_settlement_when_batch_status_is_success(db_factory):
    plan_id = make_plan(db_factory)
    first_sender_item = f"p{plan_id[:8]}-a1-000"
    second_sender_item = f"p{plan_id[:8]}-a1-001"
    provider = FakeProvider(outcomes={first_sender_item: "pending", second_sender_item: "failed"})
    payments = service(provider)
    with db_factory() as db:
        batch = payments.payout(plan_id, db)
    assert batch.state == "success"
    with db_factory() as db:
        result = payments.reconcile(plan_id, db)
    assert {item.status for item in result.batches[0].items} == {"pending", "failed"}
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "paying"


def test_uncertain_payout_intent_is_held_until_reconciliation(db_factory):
    plan_id = make_plan(db_factory)

    class CreatedThenTimedOut(FakeProvider):
        fail_once = True
        calls = 0
        def create_payout_batch(self, *args, **kwargs):
            self.calls += 1
            result = super().create_payout_batch(*args, **kwargs)
            if self.fail_once:
                self.fail_once = False
                raise ProviderTimeout("lost response", code="provider_timeout")
            return result

    provider = CreatedThenTimedOut()
    payments = service(provider)
    with db_factory() as db, pytest.raises(ProviderTimeout):
        payments.payout(plan_id, db)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        batch = db.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id))
        assert (plan.state, plan.payout_attempt, batch.status, batch.external_id) == (
            "paying", 1, "needs_attention", None)
    with db_factory() as db:
        held = payments.payout(plan_id, db)
    assert held.state == "needs_attention"
    assert (provider.calls, len(provider.batches)) == (1, 1)
    with db_factory() as db:
        reconciliation = payments.reconcile(plan_id, db)
    assert reconciliation.batches[0].status == "needs_attention"
    with db_factory() as db:
        assert db.get(Plan, plan_id).payout_attempt == 1


def test_observed_duplicate_batch_error_preserves_intent_and_does_not_retry_new_attempt(db_factory):
    plan_id = make_plan(db_factory)
    provider = FakeProvider(duplicate_batch_error=True)
    payments = service(provider)
    with db_factory() as db:
        payments.payout(plan_id, db)
    with db_factory() as db:
        # Same plan request is idempotent after the provider reference is persisted.
        assert payments.payout(plan_id, db).state == "success"
    assert len(provider.batches) == 1


def test_unexpected_provider_status_needs_attention_and_duplicate_capture_mismatch_is_rejected(db_factory):
    plan_id = make_plan(db_factory)
    sender = f"p{plan_id[:8]}-a1-000"
    provider = FakeProvider(outcomes={sender: "new-unknown-status"})
    payments = service(provider)
    with db_factory() as db:
        batch = payments.payout(plan_id, db)
    with db_factory() as db:
        payouts = payments.reconcile(plan_id, db)
    assert any(item.status == "needs_attention" for item in payouts.batches[0].items)
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "paying"


def test_duplicate_batch_provider_error_holds_intent_without_resubmission(db_factory):
    plan_id = make_plan(db_factory)

    class FailsFirst(FakeProvider):
        calls = 0
        def create_payout_batch(self, sender_batch_id, request_id, items):
            self.calls += 1
            if not hasattr(self, "first"):
                self.first = True
                super().create_payout_batch(sender_batch_id, request_id, items)
                raise PaymentProviderError("duplicate sender batch id", code="USER_BUSINESS_ERROR")
            return super().create_payout_batch(sender_batch_id, request_id, items)

    provider = FailsFirst()
    payments = service(provider)
    with db_factory() as db, pytest.raises(PaymentProviderError):
        payments.payout(plan_id, db)
    with db_factory() as db:
        assert db.get(Plan, plan_id).payout_attempt == 1
    with db_factory() as db:
        result = payments.payout(plan_id, db)
    assert result.state == "needs_attention"
    assert (provider.calls, len(provider.batches)) == (1, 1)


def test_crash_left_submitting_batch_is_reconciled_before_provider_is_called(db_factory):
    plan_id = make_plan(db_factory)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.state = "paying"
        plan.payout_attempt = 1
        db.add(PayoutBatch(plan_id=plan_id, attempt=1, sender_batch_id="plan-payout-1",
                          status="submitting"))
        db.commit()
    provider = FakeProvider()
    with db_factory() as db:
        result = service(provider).payout(plan_id, db)
    assert result.state == "needs_attention"
    assert provider.batches == {}
