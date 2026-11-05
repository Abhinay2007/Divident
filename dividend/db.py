"""SQLAlchemy engine, session factory, and database initialization."""

from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, create_engine, event
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
