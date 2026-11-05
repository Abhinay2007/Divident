import base64
import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError, PendingRollbackError

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import StateError, ValidationError
from dividend.ledger import LedgerService
from dividend.models import LedgerEvent, Plan, Session


@pytest.fixture
def db_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield engine, factory
    engine.dispose()


def add_plan(factory, state="draft"):
    with factory.begin() as db:
        session = Session(mode="demo", source_names=[], source_count=0)
        db.add(session)
        db.flush()
        plan = Plan(session_id=session.id, state=state, budget_cents=100, payable_total_cents=100,
                    reserved_total_cents=0, est_payout_fees_cents=0, charge_cents=100)
        db.add(plan)
        db.flush()
        result = (plan.id, session.id)
    return result


def add_event(factory, event_type, external_id=None, created_at=None, payload=None, **kwargs):
    with factory.begin() as db:
        row = LedgerEvent(type=event_type, payload=payload or {"x": 1}, external_id=external_id, **kwargs)
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        db.flush()
        return row.id


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def test_append_deduplicates_by_pair_null_ids_are_independent_and_never_commits(db_factory):
    _, factory = db_factory
    db = factory()
    original = LedgerService.append(db, "E", {"a": 1}, external_id="x")
    db.flush()
    duplicate = LedgerService.append(db, "E", {"a": 2}, external_id="x")
    different_type = LedgerService.append(db, "OTHER", {}, external_id="x")
    null_a = LedgerService.append(db, "E", {}, external_id=None)
    null_b = LedgerService.append(db, "E", {}, external_id=None)
    assert duplicate.id == original.id
    assert different_type.id != original.id
    assert null_a.id != null_b.id
    assert db.in_transaction()
    db.rollback()
    with factory() as check:
        assert check.scalar(select(LedgerEvent.id)) is None
    db.close()


def test_append_unique_collision_recovers_in_savepoint_and_preserves_caller_work(db_factory, monkeypatch):
    engine, factory = db_factory
    with factory.begin() as winner_session:
        winner = LedgerEvent(type="E", external_id="race", payload={"winner": True})
        winner_session.add(winner)
        winner_session.flush()
        winner_id = winner.id

    db = factory()
    unrelated = Session(mode="demo", source_names=["pending"], source_count=1)
    db.add(unrelated)
    real_scalar = db.scalar
    initial_lookup = True

    def miss_first_duplicate_lookup(statement, *args, **kwargs):
        nonlocal initial_lookup
        if initial_lookup:
            initial_lookup = False
            return None
        return real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", miss_first_duplicate_lookup)
    database_failures = []

    def capture_unique_failure(exception_context):
        if "INSERT INTO ledger_events" in (exception_context.statement or ""):
            database_failures.append(exception_context.original_exception)

    event.listen(engine, "handle_error", capture_unique_failure)
    rollbacks = []
    real_rollback = db.rollback
    monkeypatch.setattr(db, "rollback", lambda: (rollbacks.append(True), real_rollback())[1])
    try:
        recovered = LedgerService.append(db, "E", {"loser": True}, external_id="race")
        assert initial_lookup is False
        assert recovered.id == winner_id
        assert len(database_failures) == 1
        assert isinstance(database_failures[0], sqlite3.IntegrityError)
        assert rollbacks == []
        assert db.in_transaction()
        assert unrelated.id is not None  # begin_nested flushes caller-pending work before its savepoint
        db.commit()
    finally:
        event.remove(engine, "handle_error", capture_unique_failure)
        db.close()

    with factory() as check:
        assert check.get(Session, unrelated.id) is not None
        assert check.get(LedgerEvent, winner_id) is not None
        assert check.scalar(select(LedgerEvent.id).where(LedgerEvent.external_id == "race")) == winner_id


def test_append_propagates_unrelated_pending_work_integrity_error(db_factory, monkeypatch):
    _, factory = db_factory
    db = factory()
    db.add(LedgerEvent(type="INVALID_PENDING", payload={}, plan_id="missing-plan"))

    scalar_calls = []
    real_scalar = db.scalar

    def track_scalar(statement, *args, **kwargs):
        scalar_calls.append(statement)
        return real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", track_scalar)
    flush_errors = []
    real_flush = db.flush

    def track_flush(*args, **kwargs):
        try:
            return real_flush(*args, **kwargs)
        except IntegrityError as exc:
            flush_errors.append(exc)
            raise

    monkeypatch.setattr(db, "flush", track_flush)
    rollbacks = []
    real_rollback = db.rollback
    monkeypatch.setattr(db, "rollback", lambda: (rollbacks.append(True), real_rollback())[1])

    try:
        with pytest.raises(IntegrityError) as raised:
            LedgerService.append(db, "VALID_ATTEMPT", {}, external_id="not-a-duplicate")
        assert not isinstance(raised.value, PendingRollbackError)
        assert "FOREIGN KEY constraint failed" in str(raised.value.orig)
        assert flush_errors == [raised.value]
        assert len(scalar_calls) == 1  # initial duplicate lookup only; no recovery lookup
        assert rollbacks == []
    finally:
        real_rollback()  # explicit cleanup of the failed caller-owned transaction
        db.close()


def test_append_reraises_nonduplicate_savepoint_insert_failure_and_keeps_outer_transaction(db_factory, monkeypatch):
    _, factory = db_factory
    with factory() as db:
        flush_errors = []
        real_flush = db.flush

        def track_flush(*args, **kwargs):
            try:
                return real_flush(*args, **kwargs)
            except IntegrityError as exc:
                flush_errors.append(exc)
                raise

        monkeypatch.setattr(db, "flush", track_flush)
        with pytest.raises(IntegrityError) as raised:
            LedgerService.append(db, "INVALID_FK", {}, plan_id="missing-plan", external_id="no-match")
        assert not isinstance(raised.value, PendingRollbackError)
        assert "FOREIGN KEY constraint failed" in str(raised.value.orig)
        assert flush_errors == [raised.value]
        assert db.in_transaction()
        assert db.scalar(select(LedgerEvent.id).where(LedgerEvent.type == "INVALID_FK")) is None

        valid = LedgerService.append(db, "AFTER_SAVEPOINT_FAILURE", {}, external_id="valid")
        db.commit()

    with factory() as check:
        assert check.get(LedgerEvent, valid.id) is not None
        assert check.scalar(select(LedgerEvent.id).where(LedgerEvent.type == "INVALID_FK")) is None


def test_append_returns_existing_persisted_event(db_factory):
    _, factory = db_factory
    with factory.begin() as db:
        original = LedgerService.append(db, "E", {"first": 1}, external_id="id")
        db.flush()
        original_id = original.id
    with factory() as db:
        duplicate = LedgerService.append(db, "E", {"second": 2}, external_id="id")
        assert duplicate.id == original_id


def test_transition_inserts_additional_events_and_commits_once(db_factory, monkeypatch):
    _, factory = db_factory
    plan_id, session_id = add_plan(factory)
    with factory() as db:
        commits = []
        real_commit = db.commit
        monkeypatch.setattr(db, "commit", lambda: (commits.append(True), real_commit())[1])
        LedgerService.transition_plan(db, plan_id, "draft", "funding", "PLAN_APPROVED", {"charge_cents": 100}, additional_events=[
            {"type": "ORDER_CREATED", "payload": {"amount_cents": 100}, "external_id": "order"},
            {"type": "AUDIT", "payload": {}},
        ])
        rows = db.scalars(select(LedgerEvent).where(LedgerEvent.plan_id == plan_id)).all()
        assert commits == [True]
        assert len(rows) == 3
        assert {row.session_id for row in rows} == {session_id}
        assert db.get(Plan, plan_id).state == "funding"


def test_transition_invalid_stale_and_missing_errors_include_allowed_targets(db_factory):
    _, factory = db_factory
    plan_id, _ = add_plan(factory)
    expected_draft_targets = ["cancelled", "draft", "failed", "funding"]
    with factory() as db:
        with pytest.raises(StateError) as invalid:
            LedgerService.transition_plan(db, plan_id, "draft", "settled", "X", {})
        assert invalid.value.code == "invalid_plan_transition"
        assert invalid.value.current_state == "draft"
        assert invalid.value.allowed_transitions == expected_draft_targets

        with pytest.raises(StateError) as stale:
            LedgerService.transition_plan(db, plan_id, "funding", "funded", "X", {})
        assert stale.value.code == "stale_plan_state"
        assert stale.value.current_state == "draft"
        assert stale.value.allowed_transitions == expected_draft_targets

        with pytest.raises(StateError) as missing:
            LedgerService.transition_plan(db, "missing", "draft", "funding", "X", {})
        assert missing.value.code == "stale_plan_state"
        assert missing.value.current_state is None
        assert missing.value.allowed_transitions == []


def test_failed_to_failed_is_allowed_by_wildcard(db_factory):
    _, factory = db_factory
    plan_id, session_id = add_plan(factory, state="failed")
    with factory() as db:
        LedgerService.transition_plan(db, plan_id, "failed", "failed", "PLAN_FAILED", {"reason": "retry"})
        assert db.get(Plan, plan_id).state == "failed"
        event_row = db.scalar(select(LedgerEvent).where(LedgerEvent.plan_id == plan_id))
        assert event_row is not None and event_row.session_id == session_id


def test_transition_rolls_back_after_real_additional_event_integrity_failure(db_factory):
    engine, factory = db_factory
    plan_id, _ = add_plan(factory)
    failures = []

    def capture_unique_failure(exception_context):
        if "INSERT INTO ledger_events" in (exception_context.statement or ""):
            failures.append(exception_context.original_exception)

    event.listen(engine, "handle_error", capture_unique_failure)
    with factory() as db:
        with pytest.raises(IntegrityError):
            LedgerService.transition_plan(
                db,
                plan_id,
                "draft",
                "funding",
                "PRIMARY",
                {"primary": True},
                additional_events=[
                    {"type": "EXTRA", "payload": {"first": True}, "external_id": "duplicate-key"},
                    {"type": "EXTRA", "payload": {"second": True}, "external_id": "duplicate-key"},
                ],
            )
        assert len(failures) == 1
        assert isinstance(failures[0], sqlite3.IntegrityError)
        assert db.in_transaction() is False
        # A failed flush has been rolled back by transition_plan; this session can be reused.
        assert db.get(Plan, plan_id).state == "draft"
        LedgerService.append(db, "AFTER_FAILURE", {"usable": True})
        db.commit()
    event.remove(engine, "handle_error", capture_unique_failure)

    with factory() as check:
        assert check.get(Plan, plan_id).state == "draft"
        assert check.scalar(select(LedgerEvent.id).where(LedgerEvent.type == "PRIMARY")) is None
        assert check.scalar(select(LedgerEvent.id).where(LedgerEvent.type == "EXTRA")) is None
        assert check.scalar(select(LedgerEvent.id).where(LedgerEvent.type == "AFTER_FAILURE")) is not None


def test_list_filters_independently_and_combined(db_factory):
    _, factory = db_factory
    plan_a, session_a = add_plan(factory)
    plan_b, session_b = add_plan(factory)
    moment = datetime(2025, 1, 1, tzinfo=UTC)
    a_id = add_event(factory, "A", created_at=moment, plan_id=plan_a, session_id=session_a)
    b_id = add_event(factory, "B", created_at=moment + timedelta(seconds=1), plan_id=plan_b, session_id=session_b)
    other_plan_id = add_event(factory, "A", created_at=moment + timedelta(seconds=2), plan_id=plan_a, session_id=session_b)
    with factory() as db:
        plan_rows, _ = LedgerService.list(db, plan_id=plan_a)
        assert {row.id for row in plan_rows} == {a_id, other_plan_id}
        session_rows, _ = LedgerService.list(db, session_id=session_a)
        assert {row.id for row in session_rows} == {a_id}
        type_rows, _ = LedgerService.list(db, types=["A"])
        assert {row.id for row in type_rows} == {a_id, other_plan_id}
        combined, _ = LedgerService.list(db, plan_id=plan_a, session_id=session_a, types=["A"])
        assert [row.id for row in combined] == [a_id]
        assert LedgerService.list(db, plan_id="absent")[0] == []


def test_list_cursor_structure_tied_timestamps_and_page_boundaries(db_factory):
    _, factory = db_factory
    plan_id, session_id = add_plan(factory)
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    ids = [add_event(factory, "TIE", created_at=timestamp, plan_id=plan_id, session_id=session_id) for _ in range(5)]
    expected_order = sorted(ids)
    with factory() as db:
        first, cursor = LedgerService.list(db, plan_id=plan_id, limit=2)
        assert [row.id for row in first] == expected_order[:2]
        raw_cursor = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        cursor_data = json.loads(raw_cursor)
        assert set(cursor_data) == {"v", "created_at", "id"}
        assert cursor_data == {"v": 1, "created_at": first[-1].created_at.isoformat(), "id": first[-1].id}

        all_rows = list(first)
        next_cursor = cursor
        while next_cursor is not None:
            page, next_cursor = LedgerService.list(db, plan_id=plan_id, cursor=next_cursor, limit=2)
            all_rows.extend(page)
        assert [row.id for row in all_rows] == expected_order
        assert len({row.id for row in all_rows}) == len(ids)
        final_short_page, no_more = LedgerService.list(db, plan_id=plan_id, cursor=cursor, limit=10)
        assert len(final_short_page) == 3
        assert no_more is None
        assert LedgerService.list(db, plan_id="empty")[1] is None


@pytest.mark.parametrize("value", [
    "not-base64!",
    "A===",
    "eyJ",
    encode_cursor([]),
    encode_cursor({"v": 1, "created_at": 12, "id": "x"}),
    encode_cursor({"v": 1, "created_at": "2025-01-01T00:00:00+00:00", "id": 12}),
    encode_cursor({"v": 1, "created_at": "2025-01-01T00:00:00+00:00"}),
    encode_cursor({"v": 1, "created_at": "2025-01-01T00:00:00", "id": "x"}),
    encode_cursor({"v": True, "created_at": "2025-01-01T00:00:00+00:00", "id": "x"}),
    encode_cursor({"v": 2, "created_at": "2025-01-01T00:00:00+00:00", "id": "x"}),
    encode_cursor({"v": 1, "created_at": "bad", "id": "x"}),
    encode_cursor({"v": 1, "created_at": "2025-01-01T00:00:00+00:00", "id": ""}),
])
def test_list_rejects_malformed_cursors(db_factory, value):
    _, factory = db_factory
    with factory() as db, pytest.raises(ValidationError, match="Invalid or unsupported ledger cursor"):
        LedgerService.list(db, cursor=value)


def test_list_validates_cursor_before_query(db_factory, monkeypatch):
    _, factory = db_factory
    with factory() as db:
        monkeypatch.setattr(db, "scalars", lambda *args, **kwargs: pytest.fail("query should not run"))
        with pytest.raises(ValidationError):
            LedgerService.list(db, cursor="*")


def test_export_csv_streams_ordered_rows_with_contract_and_escaping(db_factory, monkeypatch):
    _, factory = db_factory
    plan_id, session_id = add_plan(factory)
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    first_id = add_event(factory, 'comma,"quote\nline', "first", timestamp, payload={"z": 1, "a": 2}, plan_id=plan_id, session_id=session_id)
    second_id = add_event(factory, "plain", "second", timestamp + timedelta(seconds=1), plan_id=plan_id, session_id=session_id)
    add_event(factory, "outside", "other", timestamp + timedelta(seconds=2))
    with factory() as db:
        result_calls = []
        original_scalars = db.scalars

        def track_streaming(query, *args, **kwargs):
            result_calls.append(query.get_execution_options())
            return original_scalars(query, *args, **kwargs)

        monkeypatch.setattr(db, "scalars", track_streaming)
        rows = LedgerService.export_csv(db, plan_id=plan_id, session_id=session_id)
        header = next(rows)
        assert header == "id,created_at,type,plan_id,session_id,external_id,payload\r\n"
        chunks = [header, *rows]

    parsed = list(csv.reader(io.StringIO("".join(chunks))))
    assert parsed[0] == ["id", "created_at", "type", "plan_id", "session_id", "external_id", "payload"]
    assert len(parsed) == 3
    assert parsed[1][2] == 'comma,"quote\nline'
    assert parsed[1][6] == '{"a":2,"z":1}'
    assert parsed[1][1].endswith("Z")
    assert [parsed[1][0], parsed[2][0]] == [first_id, second_id]
    assert result_calls[0]["yield_per"] == 100
    assert result_calls[0]["stream_results"] is True

