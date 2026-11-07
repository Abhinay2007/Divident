"""Seed an empty local database with the deterministic demo dataset."""

import argparse

from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.demo import seed_demo_if_empty
from dividend.models import Session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="sqlite:///./dividend.db")
    parser.add_argument("--sandbox-recipients-file")
    args = parser.parse_args()

    engine = create_database_engine(args.database_url)
    initialize_database(engine)
    factory = create_session_factory(engine)
    seeded = seed_demo_if_empty(factory, args.sandbox_recipients_file)
    with factory() as db:
        demo = db.scalar(select(Session).where(Session.mode == "demo").order_by(Session.created_at))
        print(f"seeded={str(seeded).lower()} session_id={demo.id if demo else ''}")
    engine.dispose()


if __name__ == "__main__":
    main()
