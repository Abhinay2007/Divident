from datetime import date

import pytest
from sqlalchemy import func, select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.demo import seed_demo_if_empty
from dividend.models import (
    LedgerEvent, Maintainer, PayoutBatch, PayoutItem, Plan, PlanLine, Session,
    SourceResolution, UpstreamProject,
)
from dividend.services.plan_service import PlanService


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'demo.db'}")
    initialize_database(engine)
    yield create_session_factory(engine)
    engine.dispose()


def logical_dataset(factory):
    with factory() as db:
        session = db.scalar(select(Session).where(Session.mode == "demo"))
        resolutions = db.scalars(select(SourceResolution).where(
            SourceResolution.session_id == session.id
        ).order_by(SourceResolution.source_name)).all()
        projects = db.scalars(select(UpstreamProject).where(
            UpstreamProject.session_id == session.id
        ).order_by(UpstreamProject.upstream_key)).all()
        maintainers = db.scalars(select(Maintainer).order_by(Maintainer.upstream_key)).all()
        return (
            session.source_binaries,
            [(r.source_name, r.upstream_key, r.method, r.outcome) for r in resolutions],
            [(p.upstream_key, p.source_names, p.binary_count, p.need_signals) for p in projects],
            [(m.upstream_key, m.email, m.opted_in) for m in maintainers],
        )


def test_demo_seed_is_idempotent_and_logically_deterministic(factory, tmp_path):
    assert seed_demo_if_empty(factory) is True
    assert seed_demo_if_empty(factory) is False
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Session)) == 1
        assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 2
        maintainers = db.scalars(select(Maintainer)).all()
        assert any(row.opted_in for row in maintainers)
        assert any(not row.opted_in for row in maintainers)
        assert all(row.display_name.startswith("Synthetic demo maintainer") for row in maintainers)
        assert all(row.email.endswith("@example.invalid") for row in maintainers)
        assert db.scalar(select(func.count()).select_from(Plan)) == 0
        assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 0
        assert db.scalar(select(func.count()).select_from(PayoutItem)) == 0

    other_engine = create_database_engine(f"sqlite:///{tmp_path / 'second.db'}")
    initialize_database(other_engine)
    other_factory = create_session_factory(other_engine)
    assert seed_demo_if_empty(other_factory) is True
    assert logical_dataset(factory) == logical_dataset(other_factory)
    other_engine.dispose()


def test_demo_relationships_eligibility_and_plan_creation(factory):
    assert seed_demo_if_empty(factory)
    with factory.begin() as db:
        session = db.scalar(select(Session).where(Session.mode == "demo"))
        resolutions = db.scalars(select(SourceResolution).where(
            SourceResolution.session_id == session.id
        )).all()
        projects = db.scalars(select(UpstreamProject).where(
            UpstreamProject.session_id == session.id
        )).all()
        maintainers = {m.upstream_key: m.opted_in for m in db.scalars(select(Maintainer))}

        assert session.status == "ready"
        assert len(session.source_binaries["apt"]) == 3
        project_by_key = {p.upstream_key: p for p in projects}
        for project in projects:
            assert project.binary_count == sum(
                len(session.source_binaries[source]) for source in project.source_names
            )
            assert all(next(r for r in resolutions if r.source_name == source).upstream_key
                       == project.upstream_key for source in project.source_names)

        by_source = {r.source_name: r for r in resolutions}
        assert by_source["linux-signed-amd64"].outcome == "no_upstream"
        assert by_source["libxcb-doc"].outcome == "unmapped"
        assert any(value is True for value in maintainers.values())
        assert any(value is False for value in maintainers.values())
        assert set(project_by_key) - set(maintainers)  # missing maintainer is reserved
        assert len({tuple(sorted(project.need_signals.items())) for project in projects}) > 1

        plan = PlanService.create(db, session.id, 2500, date(2026, 11, 6))
        lines = db.scalars(select(PlanLine).where(PlanLine.plan_id == plan.id)).all()
        assert sum(line.amount_cents for line in lines) == 2500
        assert any(line.payable_cents > 0 for line in lines)
        assert any(line.reserved_cents > 0 for line in lines)
        assert all(line.payable_cents > 0 if maintainers.get(line.upstream_key, False)
                   else line.payable_cents == 0 for line in lines)
        assert all(line.reserved_cents == 0 if maintainers.get(line.upstream_key, False)
                   else line.reserved_cents == line.amount_cents for line in lines)
        assert plan.charge_cents is None
        assert plan.est_payout_fees_cents is None
        assert plan.state == "draft"
        assert plan.order_id is None
        assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 0
        assert db.scalar(select(func.count()).select_from(PayoutItem)) == 0
