import pytest
from decimal import Decimal
from sqlalchemy import ForeignKey, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dividend.db import Base, create_database_engine, create_session_factory, initialize_database, seed_if_empty


def test_engine_and_session_lifecycle(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    factory = create_session_factory(engine)
    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()


def test_sqlite_creates_configured_parent_and_persists_data(tmp_path):
    database_path = tmp_path / "mounted" / "nested" / "dividend.db"
    url = f"sqlite:///{database_path}"
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE persistence_check (value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO persistence_check VALUES ('kept')"))
    engine.dispose()

    reopened = create_database_engine(url)
    with reopened.connect() as connection:
        assert connection.scalar(text("SELECT value FROM persistence_check")) == "kept"
    reopened.dispose()


def test_initialization_creates_registered_metadata(tmp_path):
    class TestBase(DeclarativeBase):
        pass

    class RegisteredEntity(TestBase):
        __tablename__ = "registered_entity"
        id: Mapped[int] = mapped_column(primary_key=True)

    engine = create_database_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    initialize_database(engine, TestBase.metadata)
    assert "registered_entity" in inspect(engine).get_table_names()
    engine.dispose()


def test_sqlite_wal_and_foreign_keys_on_each_connection(tmp_path):
    class ForeignKeyBase(DeclarativeBase):
        pass

    class Parent(ForeignKeyBase):
        __tablename__ = "parent"
        id: Mapped[int] = mapped_column(primary_key=True)

    class Child(ForeignKeyBase):
        __tablename__ = "child"
        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))

    engine = create_database_engine(f"sqlite:///{tmp_path / 'pragmas.db'}", pool_size=2, max_overflow=0)
    initialize_database(engine, ForeignKeyBase.metadata)
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            with pytest.raises(IntegrityError):
                connection.execute(Child.__table__.insert().values(id=1, parent_id=999))
            connection.rollback()
    engine.dispose()


def test_empty_metadata_seed_hook_is_safe_noop(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    factory = create_session_factory(engine)
    assert seed_if_empty(factory) is False
    engine.dispose()


def test_seed_if_empty_runs_callback_and_only_once(tmp_path):
    class SeedBase(DeclarativeBase):
        pass

    class SeedRecord(SeedBase):
        __tablename__ = "seed_test_records"
        id: Mapped[int] = mapped_column(primary_key=True)
        value: Mapped[str] = mapped_column()

    original_tables = dict(Base.metadata.tables)
    try:
        SeedRecord.__table__.to_metadata(Base.metadata)
        engine = create_database_engine(f"sqlite:///{tmp_path / 'seed-once.db'}")
        initialize_database(engine)
        factory = create_session_factory(engine)
        calls = []

        def seed(session):
            calls.append(True)
            session.execute(SeedRecord.__table__.insert().values(id=1, value="seeded"))

        assert seed_if_empty(factory, seed) is True
        assert seed_if_empty(factory, seed) is False
        assert calls == [True]
        engine.dispose()
    finally:
        for table_name in set(Base.metadata.tables) - set(original_tables):
            Base.metadata.remove(Base.metadata.tables[table_name])


def test_seed_if_empty_skips_populated_database(tmp_path):
    class SeedBase(DeclarativeBase):
        pass

    class ExistingRecord(SeedBase):
        __tablename__ = "seed_populated_records"
        id: Mapped[int] = mapped_column(primary_key=True)

    original_tables = dict(Base.metadata.tables)
    try:
        ExistingRecord.__table__.to_metadata(Base.metadata)
        engine = create_database_engine(f"sqlite:///{tmp_path / 'seed-populated.db'}")
        initialize_database(engine)
        factory = create_session_factory(engine)
        with factory() as session:
            session.execute(ExistingRecord.__table__.insert().values(id=1))
            session.commit()
        calls = []
        assert seed_if_empty(factory, lambda session: calls.append(session)) is False
        assert calls == []
        engine.dispose()
    finally:
        for table_name in set(Base.metadata.tables) - set(original_tables):
            Base.metadata.remove(Base.metadata.tables[table_name])


def test_initialization_errors_are_not_suppressed(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("block SQLite directory creation")
    with pytest.raises(FileExistsError):
        create_database_engine(f"sqlite:///{parent / 'db.sqlite'}")


def test_initialize_database_upgrades_existing_upstream_projects_idempotently(tmp_path):
    from dividend import models  # register application metadata

    engine = create_database_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE upstream_projects (id VARCHAR(36) PRIMARY KEY, legacy_value TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO upstream_projects (id, legacy_value) VALUES ('legacy', 'keep')"
        )
    initialize_database(engine)
    initialize_database(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("upstream_projects")}
    assert "source_facts" in columns
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, legacy_value, source_facts FROM upstream_projects WHERE id='legacy'"
        ).one()
    assert row == ("legacy", "keep", None)
    session_columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert "source_binaries" in session_columns
    engine.dispose()


def test_fresh_database_has_source_facts_column(tmp_path):
    from dividend import models  # register application metadata

    engine = create_database_engine(f"sqlite:///{tmp_path / 'fresh-with-facts.db'}")
    initialize_database(engine)
    assert "source_facts" in {column["name"] for column in inspect(engine).get_columns("upstream_projects")}
    assert "source_binaries" in {column["name"] for column in inspect(engine).get_columns("sessions")}
    engine.dispose()


def test_source_resolution_outcome_upgrade_is_additive_and_idempotent(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-resolution.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE source_resolutions (id VARCHAR(36) PRIMARY KEY, method VARCHAR(16) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO source_resolutions (id, method) VALUES ('legacy', 'none')"
        )
    initialize_database(engine)
    initialize_database(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("source_resolutions")}
    assert "outcome" in columns
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id, method, outcome FROM source_resolutions WHERE id='legacy'"
        ).one() == ("legacy", "none", "unmapped")
    engine.dispose()


def test_plan_persistence_upgrade_is_additive_idempotent_and_keeps_legacy_rows(tmp_path):
    from dividend import models

    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-plan.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        parent = models.Session(mode="upload", status="ready", source_names=["pkg"], source_count=1)
        session.add(parent)
        session.flush()
        session_id = parent.id

    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute("PRAGMA foreign_keys=OFF")
        with connection.begin():
            connection.exec_driver_sql("DROP TABLE plan_lines")
            connection.exec_driver_sql("DROP TABLE plans")
            connection.exec_driver_sql(
                """CREATE TABLE plans (
                    id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36) NOT NULL,
                    state VARCHAR(32) NOT NULL, budget_cents INTEGER NOT NULL,
                    payable_total_cents INTEGER NOT NULL, reserved_total_cents INTEGER NOT NULL,
                    est_payout_fees_cents INTEGER NOT NULL, charge_cents INTEGER NOT NULL,
                    order_id VARCHAR(64), order_attempt INTEGER NOT NULL,
                    capture_attempt INTEGER NOT NULL, payout_attempt INTEGER NOT NULL,
                    capture_gross_cents INTEGER, capture_fee_cents INTEGER,
                    capture_net_cents INTEGER, failure_reason VARCHAR(64),
                    frozen_at DATETIME, created_at DATETIME NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )"""
            )
            connection.exec_driver_sql(
                """CREATE TABLE plan_lines (
                    id VARCHAR(36) PRIMARY KEY, plan_id VARCHAR(36) NOT NULL,
                    upstream_key VARCHAR(256) NOT NULL, display_name VARCHAR(200) NOT NULL,
                    weight NUMERIC(10,4) NOT NULL, amount_cents INTEGER NOT NULL,
                    payable_cents INTEGER NOT NULL, reserved_cents INTEGER NOT NULL,
                    reason TEXT NOT NULL, FOREIGN KEY(plan_id) REFERENCES plans(id)
                )"""
            )
            connection.exec_driver_sql(
                "INSERT INTO plans VALUES ('legacy-plan', ?, 'draft', 100, 100, 0, 5, 105, NULL, 0, 0, 0, NULL, NULL, NULL, NULL, NULL, '2026-01-01 00:00:00')",
                (session_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO plan_lines VALUES ('legacy-line', 'legacy-plan', 'owner/repo', 'Legacy', 1.2345, 100, 100, 0, 'legacy reason')"
            )
        raw.execute("PRAGMA foreign_keys=ON")

    initialize_database(engine)
    initialize_database(engine)
    plan_cols = {column["name"]: column for column in inspect(engine).get_columns("plans")}
    line_cols = {column["name"]: column for column in inspect(engine).get_columns("plan_lines")}
    assert plan_cols["est_payout_fees_cents"]["nullable"] is True
    assert plan_cols["charge_cents"]["nullable"] is True
    assert "TEXT" in str(line_cols["weight"]["type"]).upper()
    assert line_cols["facts_snapshot"]["nullable"] is True
    with factory() as session:
        plan = session.get(models.Plan, "legacy-plan")
        line = session.get(models.PlanLine, "legacy-line")
        assert plan.est_payout_fees_cents == 5
        assert plan.charge_cents == 105
        assert line.weight == Decimal("1.2345")
        assert line.facts_snapshot is None
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_session_binary_membership_upgrade_preserves_legacy_session(tmp_path):
    from dividend import models

    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-session.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY, mode VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL, source_names JSON NOT NULL,
                binary_count INTEGER, source_count INTEGER NOT NULL,
                stage VARCHAR(32), progress_done INTEGER NOT NULL,
                progress_total INTEGER NOT NULL, error_message TEXT,
                created_at DATETIME NOT NULL, expires_at DATETIME
            )"""
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions VALUES ('legacy-session', 'paste', 'created', '[\"pkg\"]', NULL, 1, NULL, 0, 1, NULL, '2026-01-01 00:00:00', NULL)"
        )
    initialize_database(engine)
    initialize_database(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert "source_binaries" in columns
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, source_names, binary_count, source_binaries FROM sessions WHERE id='legacy-session'"
        ).one()
        assert row == ("legacy-session", '["pkg"]', None, None)
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()
