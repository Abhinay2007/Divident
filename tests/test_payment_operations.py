from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.models import (
    LedgerEvent, PaymentOperation, PayoutBatch, PayoutItem, Plan, Session,
)


def _database(tmp_path, name="payment-operations.db"):
    engine = create_database_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return engine, create_session_factory(engine)


def _plan(db, *, state="draft", order_attempt=0, capture_attempt=0,
          payout_attempt=0, order_id=None, capture_values=False):
    parent = Session(mode="upload", status="ready", source_names=["pkg"], source_count=1)
    db.add(parent)
    db.flush()
    plan = Plan(
        session_id=parent.id, state=state, budget_cents=100,
        payable_total_cents=100, reserved_total_cents=0,
        est_payout_fees_cents=None, charge_cents=None,
        order_attempt=order_attempt, capture_attempt=capture_attempt,
        payout_attempt=payout_attempt, order_id=order_id,
        capture_gross_cents=100 if capture_values else None,
        capture_fee_cents=2 if capture_values else None,
        capture_net_cents=98 if capture_values else None,
    )
    db.add(plan)
    db.flush()
    return plan


def test_payment_operation_schema_constraints_and_indexes(tmp_path):
    engine, factory = _database(tmp_path)
    with factory.begin() as db:
        plan = _plan(db)
        plan_id = plan.id
        db.add(PaymentOperation(
            plan_id=plan_id, kind="order", attempt=1, state="unknown",
            request_id=f"plan:{plan_id}:order:1",
        ))
    with factory() as db:
        for kwargs in (
            {"kind": "other", "attempt": 1, "state": "unknown"},
            {"kind": "order", "attempt": 0, "state": "unknown"},
            {"kind": "order", "attempt": 2, "state": "not-a-state"},
        ):
            db.add(PaymentOperation(
                plan_id=plan_id, request_id=f"invalid-{kwargs}", **kwargs,
            ))
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()
        db.add(PaymentOperation(
            plan_id=plan_id, kind="order", attempt=2, state="intent",
            request_id=f"plan:{plan_id}:order:2",
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        assert len(db.scalars(select(PaymentOperation.id)).all()) == 1

    indexes = {index["name"]: index for index in inspect(engine).get_indexes("payment_operations")}
    assert indexes["uq_payment_operations_active_kind"]["unique"] == 1
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO payment_operations "
                "(id, plan_id, kind, attempt, state, request_id, created_at, updated_at) "
                "VALUES ('bad-fk', 'missing-plan', 'order', 1, 'intent', 'bad-fk-request', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    engine.dispose()


def test_backfill_is_additive_idempotent_and_preserves_legacy_rows(tmp_path):
    engine, factory = _database(tmp_path)
    with factory.begin() as db:
        draft = _plan(db, state="draft")
        unknown_order = _plan(db, state="funding", order_attempt=1)
        known_order = _plan(db, state="funding", order_attempt=1, order_id="order-known")
        captured = _plan(db, state="funded", order_attempt=1, capture_attempt=1,
                         order_id="order-captured", capture_values=True)
        unknown_capture = _plan(db, state="funding", order_attempt=1, capture_attempt=1,
                                order_id="order-capture-unknown")
        confirmed_payout = _plan(db, state="paying", payout_attempt=1)
        uncertain_payout = _plan(db, state="paying", payout_attempt=1)
        db.add_all([
            LedgerEvent(plan_id=known_order.id, type="ORDER_CREATED",
                        external_id="order-known", payload={"order_id": "order-known"}),
            LedgerEvent(plan_id=captured.id, type="ORDER_CREATED",
                        external_id="order-captured", payload={"order_id": "order-captured"}),
            LedgerEvent(plan_id=captured.id, type="ORDER_CAPTURED", payload={
                "capture_id": "capture-known", "gross_cents": 100,
                "fee_cents": 2, "net_cents": 98,
            }),
        ])
        db.flush()
        confirmed_batch = PayoutBatch(
            plan_id=confirmed_payout.id, attempt=1, sender_batch_id="sender-confirmed",
            external_id="batch-confirmed", status="success",
        )
        uncertain_batch = PayoutBatch(
            plan_id=uncertain_payout.id, attempt=1, sender_batch_id="sender-unknown",
            status="submitting",
        )
        db.add_all([confirmed_batch, uncertain_batch])
        db.flush()
        db.add_all([
            PayoutItem(batch_id=confirmed_batch.id, plan_id=confirmed_payout.id,
                       upstream_key="one/project", sender_item_id="success-item",
                       recipient_email="hidden@example.test", amount_cents=100, status="success"),
            PayoutItem(batch_id=confirmed_batch.id, plan_id=confirmed_payout.id,
                       upstream_key="two/project", sender_item_id="unclaimed-item",
                       recipient_email="hidden@example.test", amount_cents=1, status="unclaimed"),
            PayoutItem(batch_id=uncertain_batch.id, plan_id=uncertain_payout.id,
                       upstream_key="three/project", sender_item_id="pending-item",
                       recipient_email="hidden@example.test", amount_cents=100, status="pending"),
        ])
        legacy_ids = {
            "draft": draft.id, "unknown_order": unknown_order.id,
            "known_order": known_order.id, "captured": captured.id,
            "unknown_capture": unknown_capture.id,
            "confirmed_payout": confirmed_payout.id, "uncertain_payout": uncertain_payout.id,
            "confirmed_batch": confirmed_batch.id, "uncertain_batch": uncertain_batch.id,
        }

    with engine.connect() as connection:
        snapshots = {
            "plans": connection.exec_driver_sql(
                "SELECT id, state, order_id, order_attempt, capture_attempt, payout_attempt, "
                "capture_gross_cents, capture_fee_cents, capture_net_cents FROM plans ORDER BY id"
            ).all(),
            "batches": connection.exec_driver_sql(
                "SELECT id, plan_id, attempt, sender_batch_id, external_id, status, fees_cents "
                "FROM payout_batches ORDER BY id"
            ).all(),
            "items": connection.exec_driver_sql(
                "SELECT id, batch_id, sender_item_id, amount_cents, status, external_id "
                "FROM payout_items ORDER BY id"
            ).all(),
            "ledger": connection.exec_driver_sql(
                "SELECT id, plan_id, type, external_id, payload FROM ledger_events ORDER BY id"
            ).all(),
        }

    # Simulate an older database whose model tables predate payment_operations.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE payment_operations")
    initialize_database(engine)

    with factory() as db:
        rows = list(db.scalars(select(PaymentOperation).order_by(PaymentOperation.plan_id, PaymentOperation.kind)))
        by_key = {(row.plan_id, row.kind): row for row in rows}
        assert (legacy_ids["draft"], "order") not in by_key
        assert by_key[(legacy_ids["unknown_order"], "order")].state == "unknown"
        assert by_key[(legacy_ids["known_order"], "order")].state == "confirmed"
        assert by_key[(legacy_ids["captured"], "capture")].state == "resolved"
        assert by_key[(legacy_ids["captured"], "capture")].external_id == "capture-known"
        assert by_key[(legacy_ids["unknown_capture"], "capture")].state == "unknown"
        assert by_key[(legacy_ids["confirmed_payout"], "payout")].state == "confirmed"
        assert by_key[(legacy_ids["confirmed_payout"], "payout")].prior_plan_state == "funded"
        assert by_key[(legacy_ids["uncertain_payout"], "payout")].state == "unknown"
        assert by_key[(legacy_ids["uncertain_payout"], "payout")].payout_batch_id == legacy_ids["uncertain_batch"]
        assert [item.status for item in db.scalars(select(PayoutItem).order_by(PayoutItem.sender_item_id))] == [
            "pending", "success", "unclaimed",
        ]

    with factory.begin() as db:
        existing = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == legacy_ids["unknown_order"],
            PaymentOperation.kind == "order",
        ))
        existing.error_code = "preserve-me"
    initialize_database(engine)
    initialize_database(engine)
    with factory() as db:
        existing = db.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == legacy_ids["unknown_order"],
            PaymentOperation.kind == "order",
        ))
        assert existing.error_code == "preserve-me"
        assert db.scalar(select(PaymentOperation.id).where(
            PaymentOperation.plan_id == legacy_ids["confirmed_payout"],
            PaymentOperation.kind == "payout",
        ))
        assert db.scalar(select(Plan.state).where(Plan.id == legacy_ids["captured"])) == "funded"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql(
            "SELECT id, state, order_id, order_attempt, capture_attempt, payout_attempt, "
            "capture_gross_cents, capture_fee_cents, capture_net_cents FROM plans ORDER BY id"
        ).all() == snapshots["plans"]
        assert connection.exec_driver_sql(
            "SELECT id, plan_id, attempt, sender_batch_id, external_id, status, fees_cents "
            "FROM payout_batches ORDER BY id"
        ).all() == snapshots["batches"]
        assert connection.exec_driver_sql(
            "SELECT id, batch_id, sender_item_id, amount_cents, status, external_id "
            "FROM payout_items ORDER BY id"
        ).all() == snapshots["items"]
        assert connection.exec_driver_sql(
            "SELECT id, plan_id, type, external_id, payload FROM ledger_events ORDER BY id"
        ).all() == snapshots["ledger"]
    engine.dispose()


@pytest.mark.parametrize("case", [
    "draft_with_attempt", "funding_without_attempt", "conflicting_order_ids",
    "conflicting_capture_ids", "payout_counter_gap",
])
def test_backfill_fails_closed_on_inconsistent_legacy_records(tmp_path, case):
    engine, factory = _database(tmp_path)
    with factory.begin() as db:
        if case == "draft_with_attempt":
            plan = _plan(db, state="draft", order_attempt=1)
        elif case == "funding_without_attempt":
            plan = _plan(db, state="funding", order_attempt=0)
        elif case == "conflicting_order_ids":
            plan = _plan(db, state="funding", order_attempt=1, order_id="order-a")
            db.add_all([
                LedgerEvent(plan_id=plan.id, type="ORDER_CREATED", external_id="order-a",
                            payload={"order_id": "order-a"}),
                LedgerEvent(plan_id=plan.id, type="ORDER_CREATED", external_id="order-b",
                            payload={"order_id": "order-b"}),
            ])
        elif case == "conflicting_capture_ids":
            plan = _plan(db, state="funding", order_attempt=1, capture_attempt=1,
                         order_id="order-c")
            db.add_all([
                LedgerEvent(plan_id=plan.id, type="ORDER_CAPTURED",
                            payload={"capture_id": "capture-a"}),
                LedgerEvent(plan_id=plan.id, type="FEE_ACTUALIZED",
                            external_id="capture-b", payload={}),
            ])
        else:
            plan = _plan(db, state="paying", payout_attempt=2)
            db.add(PayoutBatch(plan_id=plan.id, attempt=1,
                               sender_batch_id="sender-gap", status="intent"))

    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE payment_operations")
    with pytest.raises(RuntimeError, match="payment operation migration"):
        initialize_database(engine)
    with factory() as db:
        assert db.scalar(select(PaymentOperation.id)) is None
    engine.dispose()


def test_partial_backfill_resumes_without_overwriting_existing_operation(tmp_path):
    engine, factory = _database(tmp_path)
    with factory.begin() as db:
        known = _plan(db, state="funding", order_attempt=1, order_id="known-order")
        unknown = _plan(db, state="funding", order_attempt=1)
        db.add(LedgerEvent(plan_id=known.id, type="ORDER_CREATED", external_id="known-order",
                           payload={"order_id": "known-order"}))
        db.flush()
        plan_id = unknown.id
        db.add(PaymentOperation(
            plan_id=plan_id, kind="order", attempt=1, state="unknown",
            request_id=f"plan:{plan_id}:order:1", error_code="kept",
        ))
    initialize_database(engine)
    with factory() as db:
        operations = list(db.scalars(select(PaymentOperation).order_by(PaymentOperation.plan_id)))
        assert len(operations) == 2
        assert next(row for row in operations if row.plan_id == plan_id).error_code == "kept"
        assert next(row for row in operations if row.external_id == "known-order").state == "confirmed"
    engine.dispose()


def test_backfill_rejects_conflicting_existing_operation_without_overwriting_it(tmp_path):
    engine, factory = _database(tmp_path)
    with factory.begin() as db:
        plan = _plan(db, state="funding", order_attempt=1)
        plan_id = plan.id
        db.add(PaymentOperation(
            plan_id=plan_id, kind="order", attempt=1, state="unknown",
            request_id="wrong-request-id", error_code="preserve",
        ))
    with pytest.raises(RuntimeError, match="conflicts on request_id"):
        initialize_database(engine)
    with factory() as db:
        operation = db.scalar(select(PaymentOperation).where(PaymentOperation.plan_id == plan_id))
        assert operation.request_id == "wrong-request-id"
        assert operation.error_code == "preserve"
        assert db.scalar(select(PaymentOperation.id).where(
            PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture",
        )) is None
    engine.dispose()


def test_concurrent_sqlite_initializers_serialize_schema_and_backfill(tmp_path):
    path = tmp_path / "concurrent-init.db"
    bootstrap = create_database_engine(f"sqlite:///{path}")
    initialize_database(bootstrap)
    bootstrap.dispose()
    engines = [create_database_engine(f"sqlite:///{path}") for _ in range(2)]
    barrier = Barrier(2)

    def initialize(engine):
        barrier.wait()
        initialize_database(engine)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize, engine) for engine in engines]
            for future in futures:
                future.result()
        with engines[0].connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='payment_operations'"
            ).scalar_one() == 1
    finally:
        for engine in engines:
            engine.dispose()
