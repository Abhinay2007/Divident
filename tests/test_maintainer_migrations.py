from decimal import Decimal

from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.models import Maintainer, Plan, PlanLine, Session


def test_additive_multi_maintainer_migration_preserves_existing_rows(tmp_path):
    path = tmp_path / "legacy-maintainers.db"
    engine = create_database_engine(f"sqlite:///{path}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory.begin() as db:
        session = Session(mode="upload", status="ready", source_names=["pkg"], source_count=1)
        db.add(session)
        db.flush()
        maintainer = Maintainer(
            upstream_key="owner/repo", display_name="Maintainer",
            email="private@example.test", opted_in=True, market_code="US",
        )
        plan = Plan(session_id=session.id, state="draft", budget_cents=1000,
                    payable_total_cents=1000, reserved_total_cents=0)
        db.add_all([maintainer, plan])
        db.flush()
        line = PlanLine(plan_id=plan.id, upstream_key="owner/repo", display_name="Project",
                        weight=Decimal("1"), amount_cents=1000, payable_cents=1000,
                        reserved_cents=0, reason="fixture", facts_snapshot={})
        db.add(line)
        db.flush()
        ids = session.id, maintainer.id, plan.id, line.id

    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE maintainers RENAME TO maintainers_current")
        connection.exec_driver_sql(
            "CREATE TABLE maintainers (id VARCHAR(36) PRIMARY KEY, upstream_key VARCHAR(256) "
            "NOT NULL UNIQUE, display_name VARCHAR(200) NOT NULL, email VARCHAR(256) NOT NULL, "
            "opted_in BOOLEAN NOT NULL, market_code VARCHAR(2), created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO maintainers (id, upstream_key, display_name, email, opted_in, market_code, created_at) "
            "SELECT id, upstream_key, display_name, email, opted_in, market_code, created_at "
            "FROM maintainers_current"
        )
        connection.exec_driver_sql("DROP TABLE maintainers_current")
        connection.exec_driver_sql("ALTER TABLE plan_lines DROP COLUMN maintainer_splits")
        connection.exec_driver_sql("ALTER TABLE payout_items DROP COLUMN maintainer_id")
        connection.exec_driver_sql("DROP TABLE maintainer_recipients")

    initialize_database(engine)
    initialize_database(engine)
    with factory() as db:
        session = db.get(Session, ids[0])
        maintainer = db.get(Maintainer, ids[1])
        plan = db.get(Plan, ids[2])
        line = db.get(PlanLine, ids[3])
        assert session.id == ids[0]
        assert (maintainer.id, maintainer.email, maintainer.opted_in, maintainer.market_code) == (
            ids[1], "private@example.test", True, "US",
        )
        assert (maintainer.share_bps, maintainer.active) == (10000, True)
        assert (plan.id, plan.budget_cents, plan.state) == (ids[2], 1000, "draft")
        assert line.id == ids[3]
        assert line.maintainer_splits == [{
            "maintainer_id": ids[1], "display_name": "Maintainer",
            "share_bps": 10000, "amount_cents": 1000,
        }]
        assert db.scalar(select(Plan.id).where(Plan.id == ids[2])) == ids[2]
    engine.dispose()
