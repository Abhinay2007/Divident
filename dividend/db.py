"""SQLAlchemy engine, session factory, and database initialization."""

from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for application ORM entities registered before initialization."""


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
        _upgrade_plan_persistence(engine)


def _upgrade_plan_persistence(engine: Engine) -> None:
    """Upgrade legacy plan columns without dropping rows or inventing snapshots.

    SQLite cannot alter nullability or column affinity in place. Rebuild only
    the affected tables, with foreign-key enforcement disabled on this
    migration connection and restored immediately afterward. The copies and
    replacement DDL run in one SQLite transaction.
    """
    from . import models  # noqa: F401 - ensure model tables are registered

    plan_table = Base.metadata.tables["plans"]
    line_table = Base.metadata.tables["plan_lines"]
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if not {"plans", "plan_lines"}.issubset(tables):
            return
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
