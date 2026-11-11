from datetime import UTC, datetime
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from copy import deepcopy

import pytest
from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.config import DEFAULT_POLICY_PATH
from dividend.errors import PaymentProviderError, ProviderTimeout, StateError, ValidationError
from dividend.models import (LedgerEvent, Maintainer, MaintainerRecipient,
                             PaymentOperation, PayoutBatch, PayoutItem, Plan,
                             PlanLine, Session)
from dividend.payments.base import BatchDetail, CaptureResult, ItemDetail
from dividend.payments.fake import FakeProvider
from dividend.services.approval_proposal import freeze_proposal
from dividend.services.payment_service import PaymentService, _available_funds_cents


@pytest.fixture
def db_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'payments.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    policy_path = DEFAULT_POLICY_PATH
    factory.payment_policy_path = policy_path
    yield factory
    engine.dispose()


def make_plan(factory, state="funded", *, charge=350, payable=300, reserved=50,
              est_fees=50, recipients=True, plan_id_override=None):
    with factory() as db:
        session = Session(mode="upload", status="ready", source_names=["pkg"], source_count=1)
        db.add(session)
        db.flush()
        plan = Plan(id=plan_id_override, session_id=session.id, state=state,
                    budget_cents=payable + reserved,
                    payable_total_cents=payable, reserved_total_cents=reserved,
                    est_payout_fees_cents=est_fees, charge_cents=charge,
                    est_incoming_fee_cents=0, fee_policy_version="paypal-us-standard-v1",
                    fee_policy_snapshot={"version": "paypal-us-standard-v1", "supported": True,
                        "target_distribution_cents": payable,
                        "gross_charge_cents": charge,
                        "estimated_payout_fee_cents": est_fees,
                        "estimated_incoming_fee_cents": 0,
                        "scope": {"merchant_market": "US", "payer_market_assumption": "US",
                                  "recipient_market": "US", "currency": "USD",
                                  "checkout_method": "standard_paypal_checkout",
                                  "payout_method": "paypal_payouts_api", "pricing_tier": "standard",
                                  "currency_conversion": False, "micropayments": False,
                                  "charity_pricing": False, "negotiated_rates": False},
                        "assumptions": {"incoming_percentage": "3.49", "incoming_fixed_cents": 49,
                                        "payout_fee_per_item_cents": 25, "buffer_cents": 0,
                                        "tolerance_cents": 5}},
                    capture_gross_cents=charge if state in {"funded", "paying", "settled", "settled_with_exceptions"} else None,
                    capture_fee_cents=0 if state in {"funded", "paying", "settled", "settled_with_exceptions"} else None,
                    capture_net_cents=payable + est_fees if state in {"funded", "paying", "settled", "settled_with_exceptions"} else None,
                    frozen_at=datetime.now(UTC) if state != "draft" else None,
                    order_attempt=1 if state != "draft" else 0)
        db.add(plan)
        db.flush()
        proposal_projects = []
        for key, amount, line_payable, line_reserved in (
            ("a/project", 100, 100, 0), ("b/project", 200, 200, 0), ("c/project", 50, 0, 50),
        ):
            maintainer = Maintainer(upstream_key=key, display_name="Maintainer",
                                    email=f"{key[0]}@example.test",
                                    opted_in=recipients and key != "c/project",
                                    market_code="US" if recipients and key != "c/project" else None)
            db.add(maintainer)
            db.flush()
            splits = ([{"maintainer_id": maintainer.id, "display_name": maintainer.display_name,
                        "share_bps": 10000, "amount_cents": line_payable}]
                      if line_payable else [])
            db.add(PlanLine(plan_id=plan.id, upstream_key=key, display_name=key,
                            weight=Decimal("1"), amount_cents=amount,
                            payable_cents=line_payable, reserved_cents=line_reserved,
                            reason="fixture", facts_snapshot={}, maintainer_splits=splits))
            proposal_projects.append({
                "project_id": key, "amount_cents": amount,
                "payable_cents": line_payable, "reserved_cents": line_reserved,
                "maintainers": [{"maintainer_id": maintainer.id, "amount_cents": line_payable,
                                 "share_bps": 10000}] if line_payable else [],
                "evidence": [],
            })
        proposal = {
            "schema_version": 1, "projects": proposal_projects,
            "evidence_snapshot": {"snapshot_id": "test-snapshot", "captured_at": "2026-01-01T00:00:00Z"},
            "identity_mapping": {"mapping_id": "test-mapping", "seeded_demo": True},
            "allocation_policy_version": plan.fee_policy_version, "currency": "USD",
        }
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                   "approval_proposal": freeze_proposal(proposal)}
        if state != "draft":
            db.add(LedgerEvent(plan_id=plan.id, type="PLAN_APPROVED", payload={
                "charge_cents": charge, "order_attempt": 1,
                "approval_snapshot_sha256": plan.fee_policy_snapshot["approval_proposal"]["sha256"],
            }))
        plan_id = plan.id
        db.commit()
    return plan_id


def service(provider, db_factory):
    return PaymentService(provider, "https://dividend.example", db_factory.payment_policy_path)


_LEGACY_THIRD = (
    "cf86a05d-f5a6-43ea-ad7f-ca6936dc0219", "VD4E4K8Q7SWM4",
    "pcf86a05d-a1-000", "K9JXFBAHPXXCS", 750,
)


def seed_legacy_payout(factory, *, plan_id=None, batch_id=None, sender_item_id=None,
                       external_item_id=None, amount_cents=None, state="paying",
                       item_status="pending", keep_proposal=False):
    defaults = _LEGACY_THIRD
    plan_id = plan_id or defaults[0]
    batch_id = batch_id or defaults[1]
    sender_item_id = sender_item_id or defaults[2]
    external_item_id = external_item_id or defaults[3]
    amount_cents = amount_cents or defaults[4]
    make_plan(factory, state=state, plan_id_override=plan_id)
    with factory() as db:
        plan = db.get(Plan, plan_id)
        plan.payout_attempt = 1
        if not keep_proposal:
            plan.fee_policy_snapshot = {
                key: value for key, value in plan.fee_policy_snapshot.items()
                if key != "approval_proposal"
            }
        batch = PayoutBatch(plan_id=plan_id, attempt=1,
                            sender_batch_id=f"plan_{plan_id}_payout_1",
                            external_id=batch_id, status="pending",
                            estimated_fees_cents=25)
        db.add(batch)
        db.flush()
        db.add(PayoutItem(batch_id=batch.id, plan_id=plan_id,
                          upstream_key="github.com/curl/curl",
                          sender_item_id=sender_item_id,
                          recipient_email="legacy@example.test",
                          amount_cents=amount_cents, status=item_status,
                          external_id=external_item_id))
        db.add(PaymentOperation(
            plan_id=plan_id, kind="payout", attempt=1, state="confirmed",
            request_id=f"plan:{plan_id}:payout:1", external_id=batch_id,
            payout_batch_id=batch.id, prior_plan_state="funded",
        ))
        db.commit()


class FreshLegacyPayoutProvider(FakeProvider):
    def __init__(self, detail):
        super().__init__()
        self.detail = detail
        self.reads = 0

    def get_payout_batch(self, batch_id):
        self.reads += 1
        return self.detail


def legacy_detail(*, batch_id=None, sender_item_id=None, external_item_id=None,
                  status="SUCCESS", amount_cents=None, currency="USD", fees_cents=25):
    plan_id, known_batch, known_sender, known_external, known_amount = _LEGACY_THIRD
    return BatchDetail(
        batch_id or known_batch, "SUCCESS", fees_cents,
        [ItemDetail(sender_item_id or known_sender,
                    external_item_id or known_external, status,
                    known_amount if amount_cents is None else amount_cents,
                    currency=currency)],
    )


def test_legacy_reconciliation_records_fresh_success_and_settles_third_plan(db_factory):
    seed_legacy_payout(db_factory)
    provider = FreshLegacyPayoutProvider(legacy_detail())
    with db_factory() as db:
        result = service(provider, db_factory).reconcile(_LEGACY_THIRD[0], db)
    assert provider.reads == 1
    assert result.batches[0].status == "success"
    assert result.batches[0].items[0].status == "success"
    with db_factory() as db:
        assert db.get(Plan, _LEGACY_THIRD[0]).state == "settled"
        events = list(db.scalars(select(LedgerEvent).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0])))
    assert {event.type for event in events} >= {
        "PAYOUT_FEE_ACTUALIZED", "PAYOUT_ITEM_SUCCESS", "PLAN_SETTLED",
    }


def test_allowlisted_plan_with_valid_proposal_uses_normal_reconciliation(db_factory):
    seed_legacy_payout(db_factory, keep_proposal=True)
    provider = FreshLegacyPayoutProvider(legacy_detail(external_item_id="normal-path-item"))
    with db_factory() as db:
        result = service(provider, db_factory).reconcile(_LEGACY_THIRD[0], db)
    assert provider.reads == 1
    assert result.batches[0].items[0].status == "success"
    with db_factory() as db:
        assert db.get(Plan, _LEGACY_THIRD[0]).state == "settled"
        assert db.scalar(select(PayoutItem.external_id).where(
            PayoutItem.plan_id == _LEGACY_THIRD[0])) == "normal-path-item"


def test_allowlisted_plan_with_invalid_proposal_fails_without_writes(db_factory):
    seed_legacy_payout(db_factory)
    with db_factory() as db:
        plan = db.get(Plan, _LEGACY_THIRD[0])
        plan.fee_policy_snapshot = {
            **plan.fee_policy_snapshot,
            "approval_proposal": {"snapshot": {"schema_version": 1}, "sha256": "bad"},
        }
        db.commit()
    provider = FreshLegacyPayoutProvider(legacy_detail())
    with db_factory() as db:
        before_event_count = len(list(db.scalars(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0]))))
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider, db_factory).reconcile(_LEGACY_THIRD[0], db)
    assert error.value.code == "approval_snapshot_invalid"
    assert provider.reads == 0
    with db_factory() as db:
        assert db.get(Plan, _LEGACY_THIRD[0]).state == "paying"
        assert db.scalar(select(PayoutBatch.status).where(
            PayoutBatch.plan_id == _LEGACY_THIRD[0])) == "pending"
        assert len(list(db.scalars(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0])))) == before_event_count


def test_allowlisted_valid_proposal_provider_mismatch_has_no_writes(db_factory):
    seed_legacy_payout(db_factory, keep_proposal=True)
    provider = FreshLegacyPayoutProvider(legacy_detail(amount_cents=751))
    with db_factory() as db:
        before_event_count = len(list(db.scalars(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0]))))
    with db_factory() as db, pytest.raises(PaymentProviderError):
        service(provider, db_factory).reconcile(_LEGACY_THIRD[0], db)
    with db_factory() as db:
        assert provider.reads == 1
        assert db.get(Plan, _LEGACY_THIRD[0]).state == "paying"
        assert db.scalar(select(PayoutBatch.status).where(
            PayoutBatch.plan_id == _LEGACY_THIRD[0])) == "pending"
        assert db.scalar(select(PayoutItem.status).where(
            PayoutItem.plan_id == _LEGACY_THIRD[0])) == "pending"
        assert len(list(db.scalars(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0])))) == before_event_count


@pytest.mark.parametrize("plan_id,batch_id,sender_item_id,external_item_id,amount", [
    ("0619cc2d-8b5c-4a90-b709-0ebe07acb7f8", "SWFXHXW893NQY",
     "p0619cc2d-a1-000", "BQ3NESV9D4BS6", 750),
    ("2f313a2f-f738-476f-9af2-9e2f970b8918", "F27CU8WP649RA",
     "p2f313a2f-a1-000", "FMAPC8HZCXVCY", 1200),
])
def test_legacy_reconciliation_preserves_already_settled_plans(
        db_factory, plan_id, batch_id, sender_item_id, external_item_id, amount):
    seed_legacy_payout(db_factory, plan_id=plan_id, batch_id=batch_id,
                       sender_item_id=sender_item_id, external_item_id=external_item_id,
                       amount_cents=amount, state="settled", item_status="success")
    detail = BatchDetail(batch_id, "SUCCESS", 25, [ItemDetail(
        sender_item_id, external_item_id, "SUCCESS", amount, currency="USD")])
    provider = FreshLegacyPayoutProvider(detail)
    with db_factory() as db:
        result = service(provider, db_factory).reconcile(plan_id, db)
    assert result.batches[0].status == "success"
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "settled"
        assert db.scalar(select(PayoutItem.status).where(
            PayoutItem.plan_id == plan_id)) == "success"
        assert db.scalar(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "PLAN_SETTLED")) is None


@pytest.mark.parametrize("detail", [
    legacy_detail(batch_id="OTHER-BATCH"),
    legacy_detail(sender_item_id="wrong-sender-item"),
    legacy_detail(external_item_id="wrong-provider-item"),
    legacy_detail(status="FUTURE_STATUS"),
    legacy_detail(amount_cents=751),
    legacy_detail(currency="EUR"),
])
def test_legacy_reconciliation_mismatch_keeps_local_state_unchanged(db_factory, detail):
    seed_legacy_payout(db_factory)
    provider = FreshLegacyPayoutProvider(detail)
    with db_factory() as db, pytest.raises(PaymentProviderError):
        service(provider, db_factory).reconcile(_LEGACY_THIRD[0], db)
    with db_factory() as db:
        assert db.get(Plan, _LEGACY_THIRD[0]).state == "paying"
        assert db.scalar(select(PayoutBatch.status).where(
            PayoutBatch.plan_id == _LEGACY_THIRD[0])) == "pending"
        assert db.scalar(select(PayoutItem.status).where(
            PayoutItem.plan_id == _LEGACY_THIRD[0])) == "pending"
        assert db.scalar(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == _LEGACY_THIRD[0],
            LedgerEvent.type == "PAYOUT_ITEM_SUCCESS")) is None


def test_legacy_reconciliation_is_idempotent(db_factory):
    seed_legacy_payout(db_factory)
    provider = FreshLegacyPayoutProvider(legacy_detail())
    payments = service(provider, db_factory)
    for _ in range(2):
        with db_factory() as db:
            payments.reconcile(_LEGACY_THIRD[0], db)
    with db_factory() as db:
        for event_type in ("PAYOUT_FEE_ACTUALIZED", "PAYOUT_ITEM_SUCCESS", "PLAN_SETTLED"):
            assert db.scalar(select(LedgerEvent.id).where(
                LedgerEvent.plan_id == _LEGACY_THIRD[0],
                LedgerEvent.type == event_type)) is not None
            assert len(list(db.scalars(select(LedgerEvent.id).where(
                LedgerEvent.plan_id == _LEGACY_THIRD[0],
                LedgerEvent.type == event_type)))) == 1
    assert provider.reads == 2


def test_legacy_reconciliation_rejects_non_legacy_plan(db_factory):
    plan_id = make_plan(db_factory, state="paying")
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.fee_policy_snapshot = {
            key: value for key, value in plan.fee_policy_snapshot.items()
            if key != "approval_proposal"
        }
        db.commit()
    provider = FreshLegacyPayoutProvider(legacy_detail())
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider, db_factory).reconcile(plan_id, db)
    assert error.value.code == "approval_snapshot_invalid"
    assert provider.reads == 0
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "paying"
        assert db.scalar(select(PayoutBatch.id).where(
            PayoutBatch.plan_id == plan_id)) is None


def unmeasured_service(provider, factory):
    policy_path = factory.payment_policy_path.parent / "unmeasured-policy.yaml"
    policy_path.write_text(DEFAULT_POLICY_PATH.read_text(encoding="utf-8").replace(
        "measured: true", "measured: false"
    ), encoding="utf-8")
    return PaymentService(provider, "https://dividend.example", policy_path)


def seed_capture_operation(factory, plan_id, *, order_id="existing-order", state="unknown",
                           external_id=None, provider_result=None):
    with factory() as db:
        plan = db.get(Plan, plan_id)
        plan.order_id = order_id
        plan.approval_url = "https://paypal.example/approve"
        plan.capture_attempt = 1
        operation = PaymentOperation(
            plan_id=plan_id, kind="capture", attempt=1, state=state,
            request_id=f"plan:{plan_id}:capture:1", external_id=external_id,
            provider_result=provider_result,
        )
        db.add(operation)
        db.commit()
        return operation.id


def test_unknown_charge_blocks_approval_without_freezing_plan(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=None, est_fees=None)
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider, db_factory).approve_and_fund(plan_id, db)
    assert error.value.code == "charge_unavailable"
    assert provider.orders == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.frozen_at, plan.order_attempt) == ("draft", None, 0)


def test_unmeasured_policy_blocks_order_even_with_populated_charge(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        unmeasured_service(provider, db_factory).approve_and_fund(plan_id, db)
    assert error.value.code == "fee_policy_unmeasured"
    assert provider.orders == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.frozen_at, plan.order_attempt) == ("draft", None, 0)


def test_tampered_approved_snapshot_blocks_payout(db_factory):
    plan_id = make_plan(db_factory, "funded")
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        stored = deepcopy(plan.fee_policy_snapshot["approval_proposal"])
        proposal = stored["snapshot"]
        proposal["projects"][0]["amount_cents"] += 1
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                    "approval_proposal": {**stored, "snapshot": proposal}}
        db.commit()
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider, db_factory).payout(plan_id, db)
    assert error.value.code == "approval_snapshot_invalid"
    assert provider.batches == {}


def test_unmeasured_policy_blocks_capture_before_provider_call(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    provider = FakeProvider()
    with db_factory() as db:
        db.get(Plan, plan_id).order_id = "existing-order"
        db.commit()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        unmeasured_service(provider, db_factory).capture(plan_id, db)
    assert error.value.code == "fee_policy_unmeasured"
    assert provider.captures == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.capture_attempt, plan.state) == (0, "funding")
        assert db.scalar(select(PaymentOperation.id).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture")) is None
        assert {row.type for row in db.scalars(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id
        ))} == {"PLAN_APPROVED"}


def test_non_us_or_unreported_payer_market_blocks_new_capture(db_factory):
    from dividend.payments.base import OrderDetail

    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    with db_factory() as db:
        db.get(Plan, plan_id).order_id = "approved-order"
        db.commit()

    class NonUSProvider(FakeProvider):
        def get_order(self, order_id):
            return OrderDetail(order_id, [], "CA")

    provider = NonUSProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        service(provider, db_factory).capture(plan_id, db)
    assert error.value.code == "unsupported_transaction_scope"
    assert provider.captures == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert plan.capture_attempt == 0
        assert db.scalar(select(PaymentOperation.id).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture")) is None


def test_recovered_non_us_capture_is_recorded_but_plan_cannot_pay_out(db_factory):
    from dividend.payments.base import OrderDetail

    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    seed_capture_operation(db_factory, plan_id, state="unknown")

    class ForeignCaptureProvider(FakeProvider):
        def get_order(self, order_id):
            return OrderDetail(order_id, [CaptureResult("foreign-capture", 350, 10, 340,
                                                        "USD", "COMPLETED")], "CA")

    provider = ForeignCaptureProvider()
    with db_factory() as db:
        response = service(provider, db_factory).capture(plan_id, db)
    assert response.state == "failed"
    assert provider.captures == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.failure_reason, plan.capture_net_cents) == ("UNSUPPORTED_MARKET", 340)
        assert db.scalar(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == plan_id,
            LedgerEvent.type == "UNSUPPORTED_TRANSACTION_SCOPE")) is not None


def test_unmeasured_policy_blocks_payout_dispatch(db_factory):
    plan_id = make_plan(db_factory)
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        unmeasured_service(provider, db_factory).payout(plan_id, db)
    assert error.value.code == "fee_policy_unmeasured"
    assert provider.batches == {}


def test_unmeasured_policy_blocks_failed_item_retry(db_factory):
    plan_id = make_plan(db_factory, "settled_with_exceptions")
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.payout_attempt = 1
        batch = PayoutBatch(plan_id=plan_id, attempt=1, sender_batch_id="prior-batch",
                            external_id="prior-provider-batch", status="success")
        db.add(batch)
        db.flush()
        db.add(PayoutItem(batch_id=batch.id, plan_id=plan_id, upstream_key="a/project",
                          sender_item_id="previous-item", recipient_email="a@example.test",
                          amount_cents=100, status="failed"))
        db.commit()
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(ValidationError) as error:
        unmeasured_service(provider, db_factory).retry_payout(plan_id, db)
    assert error.value.code == "fee_policy_unmeasured"
    assert provider.batches == {}
    with db_factory() as db:
        assert db.get(Plan, plan_id).payout_attempt == 1


def test_order_duplicate_request_and_capture_are_idempotent(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)
    provider = FakeProvider()
    payments = service(provider, db_factory)
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
            result = super().capture_order(order_id, request_id)
            if self.capture_calls == 1:
                raise ProviderTimeout("response lost after acceptance", code="provider_timeout")
            return result

        def get_order(self, order_id):
            self.order_reads += 1
            return super().get_order(order_id)

    provider = CountedProvider()
    payments = service(provider, db_factory)
    with db_factory() as db:
        payments.approve_and_fund(plan_id, db)
    with db_factory() as db, pytest.raises(ProviderTimeout):
        payments.capture(plan_id, db)
    with db_factory() as db:
        assert db.get(Plan, plan_id).capture_attempt == 1
    with db_factory() as db:
        assert payments.capture(plan_id, db).state == "funded"
    assert (provider.order_reads, provider.capture_calls) == (2, 1)


def test_unknown_capture_recovery_with_no_capture_never_posts_again(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    seed_capture_operation(db_factory, plan_id)

    class CountedProvider(FakeProvider):
        reads = 0
        writes = 0

        def get_order(self, order_id):
            self.reads += 1
            return super().get_order(order_id)

        def capture_order(self, order_id, request_id):
            self.writes += 1
            return super().capture_order(order_id, request_id)

    provider = CountedProvider()
    payments = service(provider, db_factory)
    with db_factory() as db, pytest.raises(PaymentProviderError) as error:
        payments.capture(plan_id, db)
    assert error.value.code == "capture_outcome_unknown"
    assert (provider.reads, provider.writes) == (1, 0)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        operation = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture"))
        assert (plan.capture_attempt, plan.state, operation.state) == (1, "funding", "unknown")


def test_cancellation_does_not_hide_unknown_capture_history(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    operation_id = seed_capture_operation(db_factory, plan_id)
    payments = service(FakeProvider(), db_factory)
    with db_factory() as db, pytest.raises(StateError) as error:
        payments.cancel(plan_id, db)
    assert error.value.code == "capture_in_progress"
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        operation = db.get(PaymentOperation, operation_id)
        assert (plan.state, plan.capture_attempt, operation.state) == ("funding", 1, "unknown")


def test_unmeasured_capture_recovery_persists_facts_without_funding_plan(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    operation_id = seed_capture_operation(db_factory, plan_id)

    class ReadOnlyRecoveryProvider(FakeProvider):
        reads = 0
        writes = 0

        def get_order(self, order_id):
            self.reads += 1
            return super().get_order(order_id)

        def capture_order(self, order_id, request_id):
            self.writes += 1
            return super().capture_order(order_id, request_id)

    provider = ReadOnlyRecoveryProvider()
    provider.orders["existing-order"] = (plan_id, 350, "USD")
    capture = CaptureResult("existing-capture", 350, 0, 350, "USD", "COMPLETED")
    provider.captures["old-request"] = capture
    provider.capture_orders["old-request"] = "existing-order"

    with db_factory() as db:
        response = unmeasured_service(provider, db_factory).capture(plan_id, db)

    assert response.capture.capture_id == "existing-capture"
    assert response.state == "funding"
    assert (provider.reads, provider.writes) == (1, 0)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        operation = db.get(PaymentOperation, operation_id)
        event = db.scalar(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "CAPTURE_RECOVERED"))
        assert (plan.state, plan.capture_gross_cents, plan.payout_attempt) == ("funding", None, 0)
        assert (operation.state, operation.external_id) == ("resolved", "existing-capture")
        assert operation.provider_result == {
            "capture_id": "existing-capture", "gross_cents": 350, "fee_cents": 0,
            "net_cents": 350, "currency": "USD", "status": "COMPLETED",
            "payer_country_code": "US",
        }
        assert event.payload["capture_id"] == "existing-capture"
    with db_factory() as db:
        again = unmeasured_service(provider, db_factory).capture(plan_id, db)
    assert again.capture.capture_id == "existing-capture"
    assert (provider.reads, provider.writes) == (1, 0)


def test_cancellation_wins_before_capture_claim_and_prevents_capture(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.order_id = "existing-order"
        plan.approval_url = "https://paypal.example/approve"
        db.commit()
    provider = FakeProvider()
    payments = service(provider, db_factory)
    with db_factory() as db:
        payments.cancel(plan_id, db)
    with db_factory() as db, pytest.raises(StateError):
        payments.capture(plan_id, db)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.order_id, plan.approval_url, plan.capture_attempt) == (
            "cancelled", "existing-order", None, 0,
        )
        assert db.scalar(select(PaymentOperation.id).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture")) is None
        assert db.scalar(select(LedgerEvent.id).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "PLAN_CANCELLED"))
    assert provider.captures == {}


def test_capture_claim_wins_and_cancellation_is_rejected(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.order_id = "existing-order"
        plan.approval_url = "https://paypal.example/approve"
        db.commit()

    class PausingProvider(FakeProvider):
        entered = Event()
        release = Event()

        def capture_order(self, order_id, request_id):
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().capture_order(order_id, request_id)

    provider = PausingProvider()
    provider.orders["existing-order"] = (plan_id, 350, "USD")
    payments = service(provider, db_factory)

    def capture():
        with db_factory() as db:
            return payments.capture(plan_id, db)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(capture)
        assert provider.entered.wait(timeout=5)
        with db_factory() as db, pytest.raises(StateError) as error:
            payments.cancel(plan_id, db)
        assert error.value.code == "capture_in_progress"
        with db_factory() as db:
            operation = db.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture"))
            assert (operation.attempt, operation.state) == (1, "submitting")
        provider.release.set()
        assert future.result(timeout=5).state == "funded"

    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        operation = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture"))
        assert plan.state == "funded"
        assert (operation.attempt, operation.state, operation.external_id) == (
            1, "confirmed", provider.captures[next(iter(provider.captures))].capture_id,
        )


def test_two_concurrent_capture_requests_create_only_one_operation(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    with db_factory() as db:
        db.get(Plan, plan_id).order_id = "existing-order"
        db.commit()

    class PausingProvider(FakeProvider):
        entered = Event()
        release = Event()
        writes = 0

        def capture_order(self, order_id, request_id):
            self.writes += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().capture_order(order_id, request_id)

    provider = PausingProvider()
    provider.orders["existing-order"] = (plan_id, 350, "USD")
    payments = service(provider, db_factory)

    def capture():
        with db_factory() as db:
            return payments.capture(plan_id, db)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(capture)
        assert provider.entered.wait(timeout=5)
        second = executor.submit(capture)
        with pytest.raises(PaymentProviderError) as error:
            second.result(timeout=5)
        assert error.value.code == "capture_outcome_unknown"
        provider.release.set()
        assert first.result(timeout=5).state == "funded"

    with db_factory() as db:
        operations = list(db.scalars(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture")))
        assert len(operations) == 1
        assert (operations[0].attempt, operations[0].state, provider.writes) == (1, "confirmed", 1)


def test_cancel_clears_approval_url_keeps_order_id_and_never_returns_link(db_factory):
    plan_id = make_plan(db_factory, "funding", charge=350, est_fees=0)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.order_id = "remote-order-id"
        plan.approval_url = "https://paypal.example/approve"
        db.commit()
    provider = FakeProvider()
    payments = service(provider, db_factory)
    with db_factory() as db:
        payments.cancel(plan_id, db)
    with db_factory() as db, pytest.raises(StateError) as error:
        payments.approve_and_fund(plan_id, db)
    assert error.value.code == "plan_cancelled"
    assert provider.orders == {}
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.order_id, plan.approval_url) == (
            "cancelled", "remote-order-id", None,
        )


def test_uncertain_order_create_stays_funding_and_is_not_resubmitted(db_factory):
    plan_id = make_plan(db_factory, "draft", charge=350, est_fees=0)

    class CreatedThenTimedOut(FakeProvider):
        calls = 0

        def create_order(self, *args, **kwargs):
            self.calls += 1
            result = super().create_order(*args, **kwargs)
            raise ProviderTimeout("response lost", code="provider_timeout")

    provider = CreatedThenTimedOut()
    payments = service(provider, db_factory)
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
    result = service(provider, db_factory)
    with db_factory() as db:
        response = result.capture(plan_id, db)
    assert response.state == "failed"
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.failure_reason, plan.capture_net_cents) == ("FUNDING_SHORTFALL", 300)
        assert db.scalar(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "FUNDING_SHORTFALL"))


def test_payout_excludes_reserved_lines_and_retries_only_confirmed_failed_items(db_factory):
    plan_id = make_plan(db_factory)
    first_sender_item = f"p{plan_id[:8]}-a1-000"
    second_sender_item = f"p{plan_id[:8]}-a1-001"
    provider = FakeProvider(outcomes={first_sender_item: "success", second_sender_item: "failed"})
    payments = service(provider, db_factory)
    with db_factory() as db:
        batch = payments.payout(plan_id, db)
    assert batch.item_count == 2
    detail = provider.get_payout_batch(batch.batch_id)
    assert {item.amount_cents for item in detail.items} == {100, 200}
    with db_factory() as db:
        result = payments.reconcile(plan_id, db)
    assert {item.status for item in result.batches[0].items} == {"success", "failed"}
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "settled_with_exceptions"
    with db_factory() as db:
        retried = payments.retry_payout(plan_id, db)
    assert retried.item_count == 1
    assert retried.state == "success"
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "settled"
        rows = list(db.scalars(select(PayoutItem).where(PayoutItem.plan_id == plan_id)))
        batches = list(db.scalars(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)))
    assert len(rows) == 3
    assert len(batches) == 2
    assert all(row.amount_cents > 0 for row in rows)


def _configure_two_recipient_project(factory, plan_id):
    ids = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
    with factory() as db:
        plan = db.get(Plan, plan_id)
        plan.capture_net_cents = 500
        line = db.scalar(select(PlanLine).where(
            PlanLine.plan_id == plan_id, PlanLine.upstream_key == "a/project"
        ))
        line.maintainer_splits = [
            {"maintainer_id": ids[0], "display_name": "A one", "share_bps": 5000, "amount_cents": 50},
            {"maintainer_id": ids[1], "display_name": "A two", "share_bps": 5000, "amount_cents": 50},
        ]
        frozen = plan.fee_policy_snapshot["approval_proposal"]
        proposal = frozen["snapshot"]
        project = next(row for row in proposal["projects"] if row["project_id"] == "a/project")
        project["maintainers"] = [
            {"maintainer_id": ids[0], "share_bps": 5000, "amount_cents": 50},
            {"maintainer_id": ids[1], "share_bps": 5000, "amount_cents": 50},
        ]
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                    "approval_proposal": freeze_proposal(proposal)}
        approval = db.scalar(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id, LedgerEvent.type == "PLAN_APPROVED"
        ))
        approval.payload = {**approval.payload, "approval_snapshot_sha256":
                            plan.fee_policy_snapshot["approval_proposal"]["sha256"]}
        db.add_all([
            MaintainerRecipient(id=ids[0], upstream_key="a/project", display_name="A one",
                                email="one@example.test", opted_in=True, market_code="US",
                                share_bps=5000, active=True),
            MaintainerRecipient(id=ids[1], upstream_key="a/project", display_name="A two",
                                email="two@example.test", opted_in=True, market_code="US",
                                share_bps=5000, active=True),
        ])
        db.commit()
    return ids


def test_multiple_project_recipients_get_independent_items_and_failed_only_retry(db_factory):
    plan_id = make_plan(db_factory)
    recipient_ids = _configure_two_recipient_project(db_factory, plan_id)
    provider = FakeProvider(outcomes={f"p{plan_id[:8]}-a1-001": "failed"})
    payments = service(provider, db_factory)

    with db_factory() as db:
        first = payments.payout(plan_id, db)
    assert first.item_count == 3
    with db_factory() as db:
        legacy_id = db.scalar(select(Maintainer.id).where(Maintainer.upstream_key == "b/project"))
    with db_factory() as db:
        result = payments.reconcile(plan_id, db)
    first_items = result.batches[0].items
    assert len(first_items) == 3
    assert {item.maintainer_id for item in first_items} == set(recipient_ids) | {legacy_id}
    assert [item.status for item in first_items].count("failed") == 1
    assert [item.status for item in first_items].count("success") == 2
    with db_factory() as db:
        retried = payments.retry_payout(plan_id, db)
    assert retried.item_count == 1
    with db_factory() as db:
        all_items = list(db.scalars(select(PayoutItem).where(
            PayoutItem.plan_id == plan_id
        ).order_by(PayoutItem.sender_item_id)))
    assert len(all_items) == 4
    assert len({item.sender_item_id for item in all_items}) == 4
    assert sum(item.amount_cents for item in all_items if item.status == "success") == 300


def test_multiple_recipient_pending_item_stays_reserved_and_blocks_retry(db_factory):
    plan_id = make_plan(db_factory)
    _configure_two_recipient_project(db_factory, plan_id)
    provider = FakeProvider(outcomes={f"p{plan_id[:8]}-a1-001": "pending"})
    payments = service(provider, db_factory)
    with db_factory() as db:
        payments.payout(plan_id, db)
    with db_factory() as db:
        result = payments.reconcile(plan_id, db)
        assert {item.status for item in result.batches[0].items} == {"success", "pending"}
        assert _available_funds_cents(db) == 125
    with db_factory() as db:
        pending_retry = payments.retry_payout(plan_id, db)
        assert pending_retry.item_count == 3
        assert pending_retry.state == "success"
    assert len(provider.batches) == 1
    with db_factory() as db:
        assert _available_funds_cents(db) >= 0


def test_available_funds_reserves_pending_unknown_and_releases_confirmed_failure(db_factory):
    plan_id = make_plan(db_factory)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.capture_net_cents = 350
        batch = PayoutBatch(plan_id=plan_id, attempt=1, sender_batch_id="funds-batch",
                            external_id="provider-batch", status="success",
                            estimated_fees_cents=50)
        db.add(batch)
        db.flush()
        pending = PayoutItem(batch_id=batch.id, plan_id=plan_id, upstream_key="a/project",
                             sender_item_id="pending", recipient_email="a@example.test",
                             amount_cents=100, status="pending")
        unknown = PayoutItem(batch_id=batch.id, plan_id=plan_id, upstream_key="b/project",
                             sender_item_id="unknown", recipient_email="b@example.test",
                             amount_cents=200, status="needs_attention")
        db.add_all([pending, unknown])
        db.flush()
        assert _available_funds_cents(db) == 0
        pending.status = "failed"
        assert _available_funds_cents(db) == 125
        batch.fees_cents = 10  # Actual fee replaces the $0.50 estimate.
        assert _available_funds_cents(db) == 140
        unknown.status = "success"
        assert _available_funds_cents(db) == 140


def test_payout_is_rejected_when_available_funds_do_not_cover_amount_and_fee(db_factory):
    plan_id = make_plan(db_factory)
    with db_factory() as db:
        db.get(Plan, plan_id).capture_net_cents = 349
        db.commit()
    provider = FakeProvider()
    with db_factory() as db, pytest.raises(StateError) as error:
        service(provider, db_factory).payout(plan_id, db)
    assert error.value.code == "insufficient_available_funds"
    assert provider.batches == {}
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "funded"
        assert db.scalar(select(PayoutBatch.id).where(PayoutBatch.plan_id == plan_id)) is None


def test_payout_operation_is_committed_before_provider_write_and_items_are_read_back(db_factory):
    plan_id = make_plan(db_factory)

    class InspectingProvider(FakeProvider):
        reads = 0

        def create_payout_batch(self, sender_batch_id, request_id, items):
            with db_factory() as db:
                operation = db.scalar(select(PaymentOperation).where(
                    PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout"))
                batch = db.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id))
                assert (operation.state, operation.request_id, operation.payout_batch_id) == (
                    "submitting", request_id, batch.id)
                assert batch.status == "submitting"
                assert db.get(Plan, plan_id).state == "paying"
            return super().create_payout_batch(sender_batch_id, request_id, items)

        def get_payout_batch(self, batch_id):
            self.reads += 1
            return super().get_payout_batch(batch_id)

    provider = InspectingProvider()
    payments = service(provider, db_factory)
    with db_factory() as db:
        result = payments.payout(plan_id, db)
    assert result.state == "success"
    assert provider.reads == 1
    with db_factory() as db:
        operation = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout"))
        plan = db.get(Plan, plan_id)
        assert (operation.state, operation.external_id, plan.state) == (
            "confirmed", result.batch_id, "settled")


def test_pending_item_prevents_settlement_when_batch_status_is_success(db_factory):
    plan_id = make_plan(db_factory)
    first_sender_item = f"p{plan_id[:8]}-a1-000"
    second_sender_item = f"p{plan_id[:8]}-a1-001"
    provider = FakeProvider(outcomes={first_sender_item: "pending", second_sender_item: "failed"})
    payments = service(provider, db_factory)
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
    payments = service(provider, db_factory)
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
        operation = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout"))
        assert (operation.state, operation.external_id) == ("unknown", None)


def test_definitely_not_sent_payout_reuses_same_operation_ids(db_factory):
    plan_id = make_plan(db_factory)

    class NotSentOnce(FakeProvider):
        calls = []

        def create_payout_batch(self, sender_batch_id, request_id, items):
            self.calls.append((sender_batch_id, request_id, [item.sender_item_id for item in items]))
            if len(self.calls) == 1:
                raise PaymentProviderError("connection failed before send", code="provider_unavailable",
                                           detail={"outcome_unknown": False})
            return super().create_payout_batch(sender_batch_id, request_id, items)

    provider = NotSentOnce()
    payments = service(provider, db_factory)
    with db_factory() as db, pytest.raises(PaymentProviderError):
        payments.payout(plan_id, db)
    with db_factory() as db:
        operation = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout"))
        assert (operation.state, operation.attempt) == ("intent", 1)
    with db_factory() as db:
        result = payments.payout(plan_id, db)
    assert result.state == "success"
    assert len(provider.calls) == 2
    assert provider.calls[0] == provider.calls[1]


def test_observed_duplicate_batch_error_preserves_intent_and_does_not_retry_new_attempt(db_factory):
    plan_id = make_plan(db_factory)
    provider = FakeProvider(duplicate_batch_error=True)
    payments = service(provider, db_factory)
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
    payments = service(provider, db_factory)
    with db_factory() as db:
        batch = payments.payout(plan_id, db)
    with db_factory() as db:
        payouts = payments.reconcile(plan_id, db)
    assert any(item.status == "needs_attention" for item in payouts.batches[0].items)
    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "paying"


def test_reconciliation_rejects_wrong_provider_batch_identity(db_factory):
    plan_id = make_plan(db_factory)

    class WrongBatch(FakeProvider):
        reads = 0

        def get_payout_batch(self, batch_id):
            self.reads += 1
            detail = super().get_payout_batch(batch_id)
            if self.reads == 1:
                return detail
            return type(detail)("OTHER-BATCH", detail.status, detail.fees_cents, detail.items)

    provider = WrongBatch(outcomes={f"p{plan_id[:8]}-a1-000": "pending",
                                    f"p{plan_id[:8]}-a1-001": "pending"})
    payments = service(provider, db_factory)
    with db_factory() as db:
        payments.payout(plan_id, db)
    with db_factory() as db, pytest.raises(PaymentProviderError) as error:
        payments.reconcile(plan_id, db)
    assert error.value.code == "invalid_payout_response"
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
    payments = service(provider, db_factory)
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
        batch = PayoutBatch(plan_id=plan_id, attempt=1, sender_batch_id="plan-payout-1",
                            status="submitting")
        db.add(batch)
        db.flush()
        db.add(PaymentOperation(
            plan_id=plan_id, kind="payout", attempt=1, state="submitting",
            request_id=f"plan:{plan_id}:payout:1", payout_batch_id=batch.id,
            prior_plan_state="funded",
        ))
        db.commit()
    provider = FakeProvider()
    with db_factory() as db:
        result = service(provider, db_factory).payout(plan_id, db)
    assert result.state == "needs_attention"
    assert provider.batches == {}
