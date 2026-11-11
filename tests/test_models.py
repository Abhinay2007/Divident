from datetime import UTC
from uuid import UUID

import pytest
from decimal import Decimal

from sqlalchemy import BigInteger, Integer, Text, inspect
from sqlalchemy.exc import IntegrityError

import dividend.models as models
from dividend.db import Base, create_database_engine, create_session_factory, initialize_database


EXPECTED_TABLES = {
    "sessions", "source_resolutions", "upstream_projects", "maintainers", "maintainer_recipients", "plans",
    "plan_lines", "payout_batches", "payout_items", "payment_operations",
    "ledger_events", "llm_cache", "maintainer_claims",
}


@pytest.fixture
def database(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'models.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield engine, factory
    engine.dispose()


def _names(table):
    return set(table.columns.keys())


def test_import_registers_expected_tables():
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(Base.metadata.tables) == 13


def test_columns_types_and_money_are_lld_defined():
    expected = {
        "sessions": {"id", "mode", "status", "source_names", "source_binaries", "binary_count", "source_count", "stage", "progress_done", "progress_total", "error_message", "created_at", "expires_at"},
        "source_resolutions": {"id", "session_id", "source_name", "upstream_key", "method", "outcome", "evidence", "validated", "cached", "created_at"},
        "upstream_projects": {"id", "session_id", "upstream_key", "display_name", "repository_url", "binary_count", "source_names", "source_facts", "need_signals", "created_at"},
        "maintainers": {"id", "upstream_key", "display_name", "email", "opted_in", "market_code", "share_bps", "active", "created_at"},
        "maintainer_recipients": {"id", "upstream_key", "display_name", "email", "opted_in", "market_code", "share_bps", "active", "created_at"},
        "maintainer_claims": {"id", "recipient_id", "upstream_key", "status", "github_user_id", "github_login", "invitation_token_hash", "invitation_expires_at", "session_token_hash", "session_expires_at", "oauth_state_hash", "oauth_code_verifier", "oauth_state_expires_at", "created_at", "verified_at"},
        "plans": {"id", "session_id", "state", "budget_cents", "payable_total_cents", "reserved_total_cents", "est_payout_fees_cents", "est_incoming_fee_cents", "charge_cents", "fee_policy_version", "fee_policy_snapshot", "order_id", "approval_url", "order_attempt", "capture_attempt", "payout_attempt", "capture_gross_cents", "capture_fee_cents", "capture_net_cents", "failure_reason", "frozen_at", "created_at"},
        "plan_lines": {"id", "plan_id", "upstream_key", "display_name", "weight", "amount_cents", "payable_cents", "reserved_cents", "reason", "facts_snapshot", "maintainer_splits"},
        "payout_batches": {"id", "plan_id", "attempt", "sender_batch_id", "external_id", "status", "fees_cents", "estimated_fees_cents", "created_at"},
        "payout_items": {"id", "batch_id", "plan_id", "upstream_key", "maintainer_id", "sender_item_id", "recipient_email", "amount_cents", "status", "external_id", "failure_reason", "updated_at"},
        "payment_operations": {"id", "plan_id", "kind", "attempt", "state", "request_id", "external_id", "payout_batch_id", "provider_result", "prior_plan_state", "error_code", "created_at", "updated_at", "resolved_at"},
        "ledger_events": {"id", "plan_id", "session_id", "type", "external_id", "payload", "created_at"},
        "llm_cache": {"key", "response", "created_at"},
    }
    assert {name: _names(table) for name, table in Base.metadata.tables.items()} == expected
    money_columns = ["budget_cents", "payable_total_cents", "reserved_total_cents", "est_payout_fees_cents", "charge_cents", "capture_gross_cents", "capture_fee_cents", "capture_net_cents"]
    for name in money_columns:
        assert isinstance(Base.metadata.tables["plans"].c[name].type, Integer)
    assert Base.metadata.tables["plans"].c["est_payout_fees_cents"].nullable is True
    assert Base.metadata.tables["plans"].c["charge_cents"].nullable is True
    assert isinstance(Base.metadata.tables["plan_lines"].c["weight"].type.impl, Text)
    assert Base.metadata.tables["plan_lines"].c["facts_snapshot"].nullable is True
    assert isinstance(Base.metadata.tables["maintainer_claims"].c["github_user_id"].type, BigInteger)
    for table_name, column_name in (("plan_lines", "amount_cents"), ("plan_lines", "payable_cents"), ("plan_lines", "reserved_cents"), ("payout_items", "amount_cents"), ("payout_batches", "fees_cents")):
        assert isinstance(Base.metadata.tables[table_name].c[column_name].type, Integer)


def test_primary_keys_foreign_keys_unique_constraints_and_indexes():
    inspector_tables = Base.metadata.tables
    for table in inspector_tables.values():
        assert len([column for column in table.columns if column.primary_key]) >= 1
    assert {fk.target_fullname for fk in inspector_tables["maintainers"].foreign_keys} == set()
    assert {fk.target_fullname for fk in inspector_tables["source_resolutions"].foreign_keys} == {"sessions.id"}
    assert {fk.target_fullname for fk in inspector_tables["upstream_projects"].foreign_keys} == {"sessions.id"}
    assert {fk.target_fullname for fk in inspector_tables["plans"].foreign_keys} == {"sessions.id"}
    assert {fk.target_fullname for fk in inspector_tables["plan_lines"].foreign_keys} == {"plans.id"}
    assert {fk.target_fullname for fk in inspector_tables["payout_batches"].foreign_keys} == {"plans.id"}
    assert {fk.target_fullname for fk in inspector_tables["payout_items"].foreign_keys} == {"payout_batches.id", "plans.id"}
    assert {fk.target_fullname for fk in inspector_tables["payment_operations"].foreign_keys} == {"plans.id", "payout_batches.id"}
    assert {fk.target_fullname for fk in inspector_tables["ledger_events"].foreign_keys} == {"plans.id", "sessions.id"}

    expected_unique = {
        "source_resolutions": {("session_id", "source_name")},
        "upstream_projects": {("session_id", "upstream_key")},
        "payout_batches": {("plan_id", "attempt")},
        "payout_items": {("batch_id", "sender_item_id")},
        "payment_operations": {("plan_id", "kind", "attempt"), ("request_id",), ("payout_batch_id",)},
    }
    for table_name, columns in expected_unique.items():
        actual = {tuple(constraint.columns.keys()) for constraint in inspector_tables[table_name].constraints if constraint.__class__.__name__ == "UniqueConstraint"}
        assert columns <= actual
    assert inspector_tables["maintainers"].c.upstream_key.unique is True
    assert inspector_tables["maintainer_recipients"].c.upstream_key.unique is not True
    assert {"status", "created_at"} <= {column.name for index in inspector_tables["sessions"].indexes for column in index.columns}
    assert {"session_id", "state"} <= {column.name for index in inspector_tables["plans"].indexes for column in index.columns}
    assert {"plan_id", "status"} <= {column.name for index in inspector_tables["payout_items"].indexes for column in index.columns}
    assert any(tuple(index.columns.keys()) == ("plan_id", "created_at") for index in inspector_tables["ledger_events"].indexes)
    assert any(tuple(index.columns.keys()) == ("session_id", "created_at") for index in inspector_tables["ledger_events"].indexes)
    assert any(index.name == "uq_payment_operations_active_kind" and index.unique
               for index in inspector_tables["payment_operations"].indexes)


def test_partial_ledger_dedupe_index(database):
    _, factory = database
    with factory.begin() as session:
        session.add_all([
            models.LedgerEvent(type="TEST", external_id="same", payload={}),
            models.LedgerEvent(type="TEST", external_id=None, payload={}),
            models.LedgerEvent(type="TEST", external_id=None, payload={}),
        ])
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(models.LedgerEvent(type="TEST", external_id="same", payload={}))


def test_maintainer_global_unique_without_project_fk(database):
    _, factory = database
    assert not models.Maintainer.__table__.foreign_keys
    with factory.begin() as session:
        session.add(models.Maintainer(upstream_key="owner/repo", display_name="A", email="a@example.test"))
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(models.Maintainer(upstream_key="owner/repo", display_name="B", email="b@example.test"))


def test_uuid_utc_defaults_and_relationship_integrity(database):
    _, factory = database
    record_id = None
    with factory.begin() as session:
        record = models.Session(mode="demo", source_names=["pkg"], source_count=1)
        session.add(record)
        session.flush()
        assert str(UUID(record.id)) == record.id
        assert record.created_at.tzinfo is not None and record.created_at.utcoffset().total_seconds() == 0
        record_id = record.id
        source = models.SourceResolution(session_id=record.id, source_name="pkg", method="none")
        session.add(source)
        session.flush()
        assert source.session.id == record.id
        assert inspect(source).mapper.relationships["session"].direction.name == "MANYTOONE"
    with factory() as session:
        persisted = session.get(models.Session, record_id)
        assert persisted.created_at.tzinfo is not None
        assert persisted.created_at.utcoffset().total_seconds() == 0


def test_reinitialization_preserves_existing_fee_policy_snapshot(database):
    engine, factory = database
    with factory.begin() as db:
        session = models.Session(mode="upload", source_names=["pkg"], source_count=1)
        db.add(session)
        db.flush()
        plan = models.Plan(session_id=session.id, budget_cents=100, payable_total_cents=100,
                           reserved_total_cents=0, state="draft",
                           fee_policy_version="historic-policy-v0",
                           fee_policy_snapshot={"version": "historic-policy-v0", "supported": False})
        db.add(plan)
        db.flush()
        plan_id = plan.id
    initialize_database(engine)
    with factory() as db:
        plan = db.get(models.Plan, plan_id)
        assert plan.fee_policy_version == "historic-policy-v0"
        assert plan.fee_policy_snapshot == {"version": "historic-policy-v0", "supported": False}
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(models.SourceResolution(session_id="missing", source_name="x", method="none"))


def test_payout_item_updated_at_defaults_and_updates(database):
    _, factory = database
    with factory.begin() as session:
        parent = models.Session(mode="demo", source_names=[], source_count=0)
        session.add(parent)
        session.flush()
        plan = models.Plan(
            session_id=parent.id,
            budget_cents=100,
            payable_total_cents=100,
            reserved_total_cents=0,
            est_payout_fees_cents=0,
            charge_cents=100,
        )
        session.add(plan)
        session.flush()
        batch = models.PayoutBatch(plan_id=plan.id, attempt=1, sender_batch_id="batch")
        session.add(batch)
        session.flush()
        item = models.PayoutItem(
            batch_id=batch.id,
            plan_id=plan.id,
            upstream_key="owner/repo",
            sender_item_id="item-1",
            recipient_email="recipient@example.test",
            amount_cents=100,
        )
        session.add(item)
        session.flush()
        item_id = item.id
        initial_updated_at = item.updated_at
        assert initial_updated_at.tzinfo is not None
        assert initial_updated_at.utcoffset().total_seconds() == 0
    with factory.begin() as session:
        item = session.get(models.PayoutItem, item_id)
        item.status = "success"
    with factory() as session:
        updated = session.get(models.PayoutItem, item_id)
        assert updated.updated_at > initial_updated_at
        assert updated.updated_at.tzinfo is not None
        assert updated.updated_at.utcoffset().total_seconds() == 0


def test_plan_line_weight_text_type_round_trips_complete_decimal(database):
    _, factory = database
    exact = Decimal("123456789012345678901234567890.12345678901234567890")
    with factory.begin() as session:
        parent = models.Session(mode="upload", source_names=["pkg"], source_count=1)
        session.add(parent)
        session.flush()
        plan = models.Plan(
            session_id=parent.id,
            budget_cents=100,
            payable_total_cents=100,
            reserved_total_cents=0,
            est_payout_fees_cents=None,
            charge_cents=None,
        )
        session.add(plan)
        session.flush()
        line = models.PlanLine(
            plan_id=plan.id,
            upstream_key="owner/repo",
            display_name="Project",
            weight=exact,
            amount_cents=100,
            payable_cents=100,
            reserved_cents=0,
            reason="test",
            facts_snapshot=None,
        )
        session.add(line)
        session.flush()
        line_id = line.id
    with factory() as session:
        persisted = session.get(models.PlanLine, line_id)
        assert persisted.weight == exact
        assert type(persisted.weight) is Decimal
