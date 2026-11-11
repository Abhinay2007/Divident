"""SQLAlchemy engine, session factory, and database initialization."""

from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, Index, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for application ORM entities registered before initialization."""


@contextmanager
def _sqlite_immediate_transaction(engine: Engine):
    """Serialize SQLite upgrade writers across processes for this transaction."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def create_database_engine(database_url: str, **engine_options: Any) -> Engine:
    """Create an engine and configure SQLite connections for application use."""
    is_sqlite = database_url.startswith("sqlite:")
    if is_sqlite:
        engine_options.setdefault("connect_args", {"check_same_thread": False})
    engine = create_engine(database_url, **engine_options)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory with explicit transaction ownership."""
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def initialize_database(engine: Engine, metadata=None) -> None:
    """Create registered model tables; model modules must be imported first."""
    registered_metadata = metadata if metadata is not None else Base.metadata
    if engine.dialect.name == "sqlite" and registered_metadata is Base.metadata:
        with _sqlite_immediate_transaction(engine) as connection:
            registered_metadata.create_all(bind=connection)
    else:
        registered_metadata.create_all(bind=engine)
    # create_all does not evolve existing SQLite tables. Additive upgrades
    # preserve legacy rows while enabling source facts and plan persistence.
    if engine.dialect.name == "sqlite" and registered_metadata is Base.metadata:
        inspector = inspect(engine)
        if "upstream_projects" in inspector.get_table_names() and "source_facts" not in {
            column["name"] for column in inspector.get_columns("upstream_projects")
        }:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE upstream_projects ADD COLUMN source_facts JSON"
                )
        inspector = inspect(engine)
        if "sessions" in inspector.get_table_names() and "source_binaries" not in {
            column["name"] for column in inspector.get_columns("sessions")
        }:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE sessions ADD COLUMN source_binaries JSON"
                )
        inspector = inspect(engine)
        if "source_resolutions" in inspector.get_table_names() and "outcome" not in {
            column["name"] for column in inspector.get_columns("source_resolutions")
        }:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE source_resolutions ADD COLUMN outcome VARCHAR(16) NOT NULL DEFAULT 'unmapped'"
                )
        inspector = inspect(engine)
        if "plans" in inspector.get_table_names() and "approval_url" not in {
            column["name"] for column in inspector.get_columns("plans")
        }:
            with engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE plans ADD COLUMN approval_url VARCHAR(512)")
        inspector = inspect(engine)
        if "payout_items" in inspector.get_table_names() and "failure_reason" not in {
            column["name"] for column in inspector.get_columns("payout_items")
        }:
            with engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE payout_items ADD COLUMN failure_reason VARCHAR(512)")
        # These fee/accounting columns are read by the plan and operation
        # migrations below, so add them before either migration selects rows.
        inspector = inspect(engine)
        for table, definitions in {
            "sessions": {
                "synthetic_demo": "BOOLEAN NOT NULL DEFAULT 0",
                "synthetic_fixture_version": "VARCHAR(64)",
                "synthetic_policy_version": "VARCHAR(64)",
                "synthetic_run_key": "VARCHAR(64)",
            },
            "plans": {
                "est_incoming_fee_cents": "INTEGER",
                "fee_policy_version": "VARCHAR(64)",
                "fee_policy_snapshot": "JSON",
                "synthetic_demo": "BOOLEAN NOT NULL DEFAULT 0",
                "synthetic_fixture_version": "VARCHAR(64)",
                "synthetic_policy_version": "VARCHAR(64)",
            },
            "plan_lines": {"maintainer_splits": "JSON"},
            "maintainers": {
                "market_code": "VARCHAR(2)",
                "share_bps": "INTEGER NOT NULL DEFAULT 10000",
                "active": "BOOLEAN NOT NULL DEFAULT 1",
            },
            "payout_items": {"maintainer_id": "VARCHAR(36)"},
            "payout_batches": {"estimated_fees_cents": "INTEGER"},
            "payment_operations": {"synthetic_demo": "BOOLEAN NOT NULL DEFAULT 0"},
        }.items():
            if table not in inspector.get_table_names():
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            missing = [(name, sql_type) for name, sql_type in definitions.items() if name not in columns]
            if missing:
                with engine.begin() as connection:
                    for name, sql_type in missing:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                        )
                inspector = inspect(engine)
        tables = set(inspect(engine).get_table_names())
        if {"plans", "plan_lines"}.issubset(tables):
            _upgrade_plan_persistence(engine)
        if {"maintainers", "plan_lines"}.issubset(tables):
            _upgrade_maintainer_recipients(engine)
        if "payment_operations" in tables:
            _upgrade_payment_operations(engine)
    if registered_metadata is Base.metadata and "sessions" in inspect(engine).get_table_names():
        if engine.dialect.name == "sqlite":
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_synthetic_run_key "
                    "ON sessions (synthetic_run_key) WHERE synthetic_run_key IS NOT NULL"
                )
        else:
            from .models import Session as SessionModel
            Index("uq_sessions_synthetic_run_key", SessionModel.synthetic_run_key,
                  unique=True, postgresql_where=text("synthetic_run_key IS NOT NULL")).create(
                      engine, checkfirst=True
                  )


def _upgrade_maintainer_recipients(engine: Engine) -> None:
    """Freeze the legacy one-recipient plan split without rewriting old records."""
    from .models import Maintainer, PlanLine

    line_table = PlanLine.__table__
    maintainer_table = Maintainer.__table__
    with engine.begin() as connection:
        maintainers = {
            row["upstream_key"]: row
            for row in connection.execute(select(maintainer_table)).mappings()
        }
        lines = connection.execute(select(line_table).where(
            line_table.c.maintainer_splits.is_(None)
        )).mappings().all()
        for line in lines:
            row = maintainers.get(line["upstream_key"])
            splits = []
            if (line["payable_cents"] > 0 and row is not None
                    and row["opted_in"] is True and row["market_code"] == "US"):
                splits = [{
                    "maintainer_id": row["id"],
                    "display_name": row["display_name"],
                    "share_bps": 10000,
                    "amount_cents": line["payable_cents"],
                }]
            connection.execute(
                line_table.update().where(line_table.c.id == line["id"])
                .values(maintainer_splits=splits)
            )


def _upgrade_payment_operations(engine: Engine) -> None:
    """Backfill durable operation identities without changing legacy rows."""
    from . import models  # noqa: F401 - ensure model tables are registered

    operations = models.PaymentOperation.__table__
    required_columns = set(operations.c.keys())
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("payment_operations")}
    if columns != required_columns:
        raise RuntimeError("payment operation migration: unexpected payment_operations columns")
    indexes = {index["name"]: index for index in inspector.get_indexes("payment_operations")}
    required_indexes = {index.name for index in operations.indexes}
    if not required_indexes.issubset(indexes):
        raise RuntimeError("payment operation migration: required payment_operations indexes are missing")
    foreign_keys = inspector.get_foreign_keys("payment_operations")
    fk_targets = {(fk["constrained_columns"][0], fk["referred_table"]) for fk in foreign_keys}
    if not {("plan_id", "plans"), ("payout_batch_id", "payout_batches")}.issubset(fk_targets):
        raise RuntimeError("payment operation migration: required foreign keys are missing")

    plan_table = models.Plan.__table__
    payout_table = models.PayoutBatch.__table__
    ledger_table = models.LedgerEvent.__table__
    now = datetime.now(UTC)

    def issue(identity: str, reason: str) -> RuntimeError:
        return RuntimeError(f"payment operation migration: {identity}: {reason}")

    with _sqlite_immediate_transaction(engine) as connection:
        plans = connection.execute(select(plan_table)).mappings().all()
        batches = connection.execute(select(payout_table)).mappings().all()
        events = connection.execute(select(ledger_table)).mappings().all()
        existing_rows = connection.execute(select(operations)).mappings().all()

        existing = {}
        for row in existing_rows:
            key = (row["plan_id"], row["kind"], row["attempt"])
            if key in existing:
                raise issue(f"plan {row['plan_id']}", "duplicate operation identity")
            existing[key] = row

        events_by_plan = {}
        for row in events:
            events_by_plan.setdefault(row["plan_id"], []).append(row)
        batches_by_plan = {}
        for row in batches:
            batches_by_plan.setdefault(row["plan_id"], []).append(row)

        expected = []
        for plan in plans:
            plan_id = plan["id"]
            order_attempt = plan["order_attempt"]
            capture_attempt = plan["capture_attempt"]
            payout_attempt = plan["payout_attempt"]
            if any(type(value) is not int or value < 0
                   for value in (order_attempt, capture_attempt, payout_attempt)):
                raise issue(f"plan {plan_id}", "invalid payment attempt counter")

            plan_events = events_by_plan.get(plan_id, [])
            order_ids = set()
            for event_row in plan_events:
                payload = event_row["payload"] if isinstance(event_row["payload"], dict) else {}
                if event_row["type"] == "ORDER_CREATED":
                    for value in (event_row["external_id"], payload.get("order_id")):
                        if isinstance(value, str) and value:
                            order_ids.add(value)
            if len(order_ids) > 1:
                raise issue(f"plan {plan_id}", "conflicting order IDs in ledger")
            ledger_order_id = next(iter(order_ids), None)
            order_id = plan["order_id"]
            if order_id and ledger_order_id and order_id != ledger_order_id:
                raise issue(f"plan {plan_id}", "stored order ID conflicts with ledger")
            known_order_id = order_id or ledger_order_id
            if plan["state"] == "draft" and (known_order_id or order_attempt != 0):
                raise issue(f"plan {plan_id}", "draft plan has order history")
            if plan["state"] == "funding" and not known_order_id and order_attempt == 0:
                raise issue(f"plan {plan_id}", "funding plan has no order attempt")
            if known_order_id and order_attempt == 0:
                raise issue(f"plan {plan_id}", "order identity has no attempt")
            if order_attempt:
                for attempt in range(1, order_attempt):
                    if (plan_id, "order", attempt) not in existing:
                        raise issue(f"plan {plan_id}", "prior order attempt identity cannot be reconstructed")
                order_state = "confirmed" if known_order_id else "unknown"
                expected.append({
                    "plan_id": plan_id, "kind": "order", "attempt": order_attempt,
                    "state": order_state,
                    "request_id": f"plan:{plan_id}:order:{order_attempt}",
                    "external_id": known_order_id, "payout_batch_id": None,
                    "provider_result": None, "prior_plan_state": None,
                    "error_code": None, "created_at": now, "updated_at": now,
                    "resolved_at": now if known_order_id else None,
                })

            capture_ids = set()
            for event_row in plan_events:
                payload = event_row["payload"] if isinstance(event_row["payload"], dict) else {}
                if event_row["type"] in {"ORDER_CAPTURED", "CAPTURE_MISMATCH"}:
                    value = payload.get("capture_id")
                    if isinstance(value, str) and value:
                        capture_ids.add(value)
                    if isinstance(event_row["external_id"], str) and event_row["external_id"]:
                        capture_ids.add(event_row["external_id"])
                elif event_row["type"] in {"FEE_ACTUALIZED", "SURPLUS_RETAINED"}:
                    if isinstance(event_row["external_id"], str) and event_row["external_id"]:
                        capture_ids.add(event_row["external_id"])
            if len(capture_ids) > 1:
                raise issue(f"plan {plan_id}", "conflicting capture IDs in ledger")
            capture_id = next(iter(capture_ids), None)
            has_capture_values = any(plan[name] is not None for name in (
                "capture_gross_cents", "capture_fee_cents", "capture_net_cents"))
            if capture_id and not known_order_id:
                raise issue(f"plan {plan_id}", "capture identity has no order identity")
            if (capture_id or has_capture_values) and capture_attempt == 0:
                raise issue(f"plan {plan_id}", "capture facts have no capture attempt")
            if capture_attempt:
                for attempt in range(1, capture_attempt):
                    if (plan_id, "capture", attempt) not in existing:
                        raise issue(f"plan {plan_id}", "prior capture attempt identity cannot be reconstructed")
                resolved = capture_id is not None
                result = ({
                    "capture_id": capture_id,
                    "gross_cents": plan["capture_gross_cents"],
                    "fee_cents": plan["capture_fee_cents"],
                    "net_cents": plan["capture_net_cents"],
                } if resolved else None)
                expected.append({
                    "plan_id": plan_id, "kind": "capture", "attempt": capture_attempt,
                    "state": "resolved" if resolved else "unknown",
                    "request_id": f"plan:{plan_id}:capture:{capture_attempt}",
                    "external_id": capture_id, "payout_batch_id": None,
                    "provider_result": result, "prior_plan_state": None,
                    "error_code": None, "created_at": now, "updated_at": now,
                    "resolved_at": now if resolved else None,
                })

            plan_batches = sorted(batches_by_plan.get(plan_id, []), key=lambda row: row["attempt"])
            batch_attempts = [row["attempt"] for row in plan_batches]
            if any(type(attempt) is not int or attempt <= 0 for attempt in batch_attempts):
                raise issue(f"plan {plan_id}", "invalid payout batch attempt")
            if batch_attempts != list(range(1, payout_attempt + 1)):
                if payout_attempt or batch_attempts:
                    raise issue(f"plan {plan_id}", "payout counter and batch attempts are inconsistent")
            for batch in plan_batches:
                attempt = batch["attempt"]
                prior_state = "funded" if attempt == 1 else "settled_with_exceptions"
                expected.append({
                    "plan_id": plan_id, "kind": "payout", "attempt": attempt,
                    "state": "confirmed" if batch["external_id"] else "unknown",
                    "request_id": f"plan:{plan_id}:payout:{attempt}",
                    "external_id": batch["external_id"], "payout_batch_id": batch["id"],
                    "provider_result": None, "prior_plan_state": prior_state,
                    "error_code": None, "created_at": now, "updated_at": now,
                    "resolved_at": now if batch["external_id"] else None,
                })

        expected_keys = set()
        for values in expected:
            key = (values["plan_id"], values["kind"], values["attempt"])
            expected_keys.add(key)
            old = existing.get(key)
            if old is None:
                continue
            for name in ("request_id", "payout_batch_id"):
                if old[name] != values[name]:
                    raise issue(f"plan {values['plan_id']}", f"existing {values['kind']} operation conflicts on {name}")
            if values["external_id"] and old["external_id"] != values["external_id"]:
                raise issue(f"plan {values['plan_id']}", f"existing {values['kind']} operation conflicts on external_id")
            if values["prior_plan_state"] and old["prior_plan_state"] not in (None, values["prior_plan_state"]):
                raise issue(f"plan {values['plan_id']}", "existing payout operation conflicts on prior_plan_state")

        plans_by_id = {row["id"]: row for row in plans}
        batches_by_id = {row["id"]: row for row in batches}
        for key, old in existing.items():
            plan = plans_by_id.get(old["plan_id"])
            if plan is None:
                raise issue(f"operation {old['id']}", "referenced plan does not exist")
            expected_request_id = f"plan:{old['plan_id']}:{old['kind']}:{old['attempt']}"
            if old["request_id"] != expected_request_id:
                raise issue(f"operation {old['id']}", "request ID does not match operation identity")
            counter = {"order": plan["order_attempt"], "capture": plan["capture_attempt"],
                       "payout": plan["payout_attempt"]}.get(old["kind"])
            if counter is None or old["attempt"] > counter:
                raise issue(f"operation {old['id']}", "operation is ahead of its plan attempt counter")
            if old["kind"] == "payout":
                matching = batches_by_id.get(old["payout_batch_id"])
                if (matching is None or matching["plan_id"] != old["plan_id"]
                        or matching["attempt"] != old["attempt"]):
                    raise issue(f"operation {old['id']}", "payout batch link does not match plan attempt")
            elif old["payout_batch_id"] is not None:
                raise issue(f"operation {old['id']}", "non-payout operation has a payout batch link")

        for values in expected:
            key = (values["plan_id"], values["kind"], values["attempt"])
            if key not in existing:
                connection.execute(operations.insert().values(**values))


def _upgrade_plan_persistence(engine: Engine) -> None:
    """Upgrade legacy plan columns without dropping rows or inventing snapshots.

    SQLite cannot alter nullability or column affinity in place. Rebuild only
    the affected tables, with foreign-key enforcement disabled on this
    migration connection and restored immediately afterward. The copies and
    replacement DDL run in one SQLite transaction.
    """
    from . import models  # noqa: F401 - ensure model tables are registered

    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if not {"plans", "plan_lines"}.issubset(tables):
            return
        plan_table = Base.metadata.tables["plans"]
        line_table = Base.metadata.tables["plan_lines"]
        plan_columns = {column["name"]: column for column in inspector.get_columns("plans")}
        line_columns = {column["name"]: column for column in inspector.get_columns("plan_lines")}
        rebuild_plans = any(
            name not in plan_columns or not plan_columns[name]["nullable"]
            for name in ("est_payout_fees_cents", "charge_cents")
        )
        rebuild_lines = (
            "weight" not in line_columns
            or "TEXT" not in str(line_columns["weight"]["type"]).upper()
            or "facts_snapshot" not in line_columns
            or not line_columns["facts_snapshot"]["nullable"]
        )
        if not rebuild_plans and not rebuild_lines:
            return

        # Inspection autobegins a SQLAlchemy transaction. End that read-only
        # transaction before toggling SQLite's per-connection FK pragma.
        connection.rollback()
        raw_connection = connection.connection.driver_connection
        raw_connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with connection.begin():
                if rebuild_plans:
                    _rebuild_sqlite_table(connection, "plans", plan_table)
                if rebuild_lines:
                    _rebuild_sqlite_table(connection, "plan_lines", line_table)
        finally:
            if connection.in_transaction():
                connection.rollback()
            raw_connection.execute("PRAGMA foreign_keys=ON")


def _rebuild_sqlite_table(connection, name: str, table) -> None:
    """Replace one table while preserving all existing common column values."""
    columns = {column["name"] for column in inspect(connection).get_columns(name)}
    legacy_name = f"__legacy_{name}_t322"
    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{legacy_name}"')
    connection.exec_driver_sql(
        f'CREATE TABLE "{legacy_name}" AS SELECT * FROM "{name}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{name}"')
    table.create(connection, checkfirst=False)

    target_columns = [column.name for column in table.columns]
    present_columns = [column for column in target_columns if column in columns]
    if name == "plan_lines" and "facts_snapshot" not in columns:
        present_columns.append("facts_snapshot")
    targets = ", ".join(f'"{column}"' for column in present_columns)
    select_values = []
    for column in present_columns:
        if column == "facts_snapshot" and column not in columns:
            select_values.append("NULL")
        elif name == "plan_lines" and column == "weight":
            select_values.append('CAST("weight" AS TEXT)')
        else:
            select_values.append(f'"{column}"')
    values = ", ".join(select_values)
    connection.exec_driver_sql(
        f'INSERT INTO "{name}" ({targets}) SELECT {values} FROM "{legacy_name}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{legacy_name}"')


def seed_if_empty(
    session_factory: sessionmaker[Session],
    seed_callback: Callable[[Session], Any] | None = None,
) -> bool:
    """Run a supplied seeder only when registered application tables are empty.

    Entity-specific seed selection belongs with the model/demo-fixture milestone.
    Until then callers may provide a seeder explicitly; an absent seeder is a
    no-op and does not invent tables or records.
    """
    if seed_callback is None:
        return False
    with session_factory() as session:
        bind = session.get_bind()
        tables = list(Base.metadata.sorted_tables)
        if not tables:
            return False
        from sqlalchemy import inspect

        inspector = inspect(bind)
        registered_names = {table.name for table in tables}
        if not registered_names.issubset(set(inspector.get_table_names())):
            return False
        for table in tables:
            if session.execute(table.select().limit(1)).first() is not None:
                return False
        seed_callback(session)
        session.commit()
        return True
