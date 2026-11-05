import pytest
from sqlalchemy import ForeignKey, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dividend.db import Base, create_database_engine, create_session_factory, initialize_database, seed_if_empty


def test_engine_and_session_lifecycle(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    factory = create_session_factory(engine)
    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()


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
    engine = create_database_engine(f"sqlite:///{tmp_path / 'missing' / 'db.sqlite'}")
    try:
        try:
            initialize_database(engine)
        except OperationalError:
            pass
        else:
            raise AssertionError("initialization unexpectedly succeeded")
    finally:
        engine.dispose()
