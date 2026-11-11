from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import func, select

from dividend.allocation import AllocationError
from dividend.allocation import facts_snapshot_from_dict
from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import NotFoundError, StateError, ValidationError
from dividend.models import (
    LedgerEvent, Maintainer, MaintainerClaim, MaintainerRecipient, Plan, PlanLine, Session as SessionRecord,
    SourceResolution, UpstreamProject,
)
from dividend.services import AllocationInputSet, PlanService
from dividend.services.plan_service import MaintainerShare, split_cents_by_share


@pytest.fixture
def db_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'plan-inputs.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def add_session(db, source_names, *, status="ready"):
    session = SessionRecord(
        mode="upload",
        status=status,
        source_names=list(source_names),
        source_count=len(source_names),
        binary_count=len(source_names),
    )
    db.add(session)
    db.flush()
    return session


def source_facts(source_name, *, partial=False, original_last_commit="2025-01-02T03:04:05+02:00"):
    return {
        source_name: {
            "source_name": source_name,
            "repository_url": f"https://github.com/example/{source_name}",
            "homepage": None,
            "donation": None,
            "github": {
                "full_name": f"example/{source_name}",
                "archived": False,
                "last_commit": original_last_commit,
                "committers_12m": 2,
                "funding_yml": True,
            },
            "partial": partial,
        }
    }


def add_resolved_project(
    db,
    session_id,
    key,
    source_name,
    *,
    binary_count=1,
    partial=False,
    opted_in=None,
    source_facts_value="present",
    normalized_last_commit="2025-01-02",
    method="repo_url",
    validated=True,
):
    if source_facts_value == "present":
        persisted_source_facts = source_facts(source_name, partial=partial)
    elif source_facts_value == "null":
        persisted_source_facts = None
    else:
        persisted_source_facts = source_facts_value
    project = UpstreamProject(
        session_id=session_id,
        upstream_key=key,
        display_name=f"Project {key}",
        repository_url=f"https://github.com/example/{key}" if key else None,
        binary_count=binary_count,
        source_names=[source_name],
        source_facts=persisted_source_facts,
        need_signals={
            "archived": False,
            "last_commit": normalized_last_commit,
            "committers_12m": 2,
            "funding_yml": True,
            "partial": partial,
        },
    )
    db.add(project)
    db.add(SourceResolution(
        session_id=session_id,
        source_name=source_name,
        upstream_key=key,
        method=method,
        validated=validated,
        cached=False,
        evidence=[{"field": "udd.repository", "value": project.repository_url}]
        if project.repository_url else None,
    ))
    if opted_in is not None:
        db.add(Maintainer(
            upstream_key=key,
            display_name=f"Maintainer {key}",
            email="maintainer@example.test",
            opted_in=opted_in,
            market_code="US" if opted_in else None,
        ))
    db.flush()
    return project


def add_unresolved(db, session_id, source_name, *, method="none", upstream_key=None):
    db.add(SourceResolution(
        session_id=session_id,
        source_name=source_name,
        upstream_key=upstream_key,
        method=method,
        validated=False,
        cached=False,
    ))


def add_allocation_session(db, *, project_count=7, opted_in=True):
    names = [f"pkg-{index}" for index in range(project_count)]
    session = add_session(db, names)
    for index, source_name in enumerate(names):
        add_resolved_project(
            db,
            session.id,
            f"owner/project-{index}",
            source_name,
            binary_count=index + 1,
            opted_in=opted_in,
        )
    return session


def test_assembles_sorted_domain_inputs_snapshots_and_opt_in_without_writes(db_factory, monkeypatch):
    evaluation_date = date(2026, 11, 6)
    with db_factory() as db:
        session = add_session(db, ["z-src", "a-src", "unmapped", "no-upstream"])
        add_resolved_project(db, session.id, "z/repo", "z-src", opted_in=False)
        add_resolved_project(db, session.id, "a/repo", "a-src", opted_in=True, partial=True)
        add_unresolved(db, session.id, "unmapped")
        add_unresolved(db, session.id, "no-upstream")
        db.commit()

    with db_factory() as db:
        monkeypatch.setattr(db, "commit", lambda: pytest.fail("assembly must not commit"))
        assembled = PlanService.assemble_allocation_inputs(db, session.id, evaluation_date)

        assert isinstance(assembled, AllocationInputSet)
        assert assembled.evaluation_date is evaluation_date
        assert [item.upstream_key for item in assembled.projects] == ["a/repo", "z/repo"]
        assert all(type(item).__name__ == "ProjectInput" for item in assembled.projects)
        opted, reserved = assembled.projects
        assert opted.opted_in is True
        assert reserved.opted_in is False
        assert opted.facts.partial is True
        assert opted.facts.snapshot.sources[0].partial is True
        assert opted.facts.snapshot.sources[0].github.last_commit == "2025-01-02T03:04:05+02:00"
        assert opted.facts.last_commit == date(2025, 1, 2)
        assert opted.facts.funding_yml is True
        assert db.scalar(select(func.count()).select_from(Plan)) == 0
        assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 0


def test_missing_maintainer_is_not_opted_in(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        add_resolved_project(db, session.id, "owner/repo", "pkg")
        db.commit()

    with db_factory() as db:
        assembled = PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))
    assert assembled.projects[0].opted_in is False


def test_only_resolved_project_records_are_eligible(db_factory):
    with db_factory() as db:
        session = add_session(db, ["resolved", "unmapped", "no-upstream"])
        add_resolved_project(db, session.id, "owner/repo", "resolved", opted_in=True)
        add_unresolved(db, session.id, "unmapped", method="none")
        add_unresolved(db, session.id, "no-upstream", method="none")
        db.commit()

    with db_factory() as db:
        result = PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))
    assert [project.upstream_key for project in result.projects] == ["owner/repo"]


def test_persisted_unverified_project_is_excluded_from_funding(db_factory):
    with db_factory() as db:
        session = add_session(db, ["candidate"])
        add_resolved_project(db, session.id, "owner/candidate", "candidate", validated=False)
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="no eligible"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_no_eligible_projects_are_rejected_before_allocation(db_factory):
    with db_factory() as db:
        session = add_session(db, ["unmapped", "no-upstream"])
        add_unresolved(db, session.id, "unmapped")
        add_unresolved(db, session.id, "no-upstream")
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="no eligible"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_missing_resolution_for_project_source_fails_closed(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        db.add(UpstreamProject(
            session_id=session.id,
            upstream_key="owner/repo",
            display_name="Project",
            binary_count=1,
            source_names=["pkg"],
            source_facts=source_facts("pkg"),
            need_signals={"archived": False, "last_commit": None, "committers_12m": None,
                          "funding_yml": None, "partial": False},
        ))
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="missing one or more source resolutions"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_unverified_candidate_without_project_is_excluded(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        add_unresolved(db, session.id, "pkg", method="repo_url", upstream_key="owner/repo")
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="no eligible"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_invalid_upstream_identity_fails_without_returning_partial_inputs(db_factory):
    with db_factory() as db:
        session = add_session(db, ["good", "bad"])
        add_resolved_project(db, session.id, "owner/good", "good")
        add_resolved_project(db, session.id, "", "bad")
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="invalid upstream key"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_duplicate_source_membership_fails_explicitly(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        project = add_resolved_project(db, session.id, "owner/repo", "pkg")
        project.source_names = ["pkg", "pkg"]
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="duplicate source name"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_legacy_null_and_invalid_persisted_source_facts_are_rejected(db_factory):
    with db_factory() as db:
        session = add_session(db, ["legacy", "invalid"])
        add_resolved_project(db, session.id, "owner/legacy", "legacy", source_facts_value="null")
        add_resolved_project(db, session.id, "owner/invalid", "invalid", source_facts_value={"bad": True})
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="invalid persisted allocation facts"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_invalid_llm_resolution_is_rejected(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        add_resolved_project(
            db, session.id, "owner/repo", "pkg", method="llm", validated=False,
        )
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="not validated"):
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))


def test_evaluation_date_must_be_explicit_strict_date(db_factory):
    with db_factory() as db:
        session = add_session(db, ["pkg"])
        add_resolved_project(db, session.id, "owner/repo", "pkg")
        db.commit()

    with db_factory() as db, pytest.raises(AllocationError, match="evaluation_date"):
        PlanService.assemble_allocation_inputs(
            db, session.id, datetime(2026, 11, 6, tzinfo=UTC),
        )

    explicit_date = date(2001, 2, 3)
    with db_factory() as db:
        result = PlanService.assemble_allocation_inputs(db, session.id, explicit_date)
    assert result.evaluation_date is explicit_date


def test_missing_or_nonready_session_fails_with_domain_errors(db_factory):
    with db_factory() as db, pytest.raises(NotFoundError) as missing:
        PlanService.assemble_allocation_inputs(db, "missing", date(2026, 11, 6))
    assert missing.value.code == "session_not_found"

    with db_factory() as db:
        session = add_session(db, [], status="resolving")
        db.commit()
    with db_factory() as db, pytest.raises(StateError) as not_ready:
        PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))
    assert not_ready.value.code == "session_not_ready"


def test_create_persists_plan_lines_snapshots_exact_weights_and_created_event(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        plan_id = plan.id
        assert plan.state == "draft"
        assert plan.est_payout_fees_cents == 25 * sum(line.payable_cents > 0 for line in plan.lines)
        assert plan.charge_cents is None
        assert plan.fee_policy_version == "paypal-us-standard-v1"
        assert plan.fee_policy_snapshot["target_distribution_cents"] == plan.payable_total_cents
        assert sum(line.amount_cents for line in plan.lines) == 1000
        assert all(line.payable_cents + line.reserved_cents == line.amount_cents for line in plan.lines)
        assert [line.upstream_key for line in plan.lines] == sorted(line.upstream_key for line in plan.lines)
        assert len(plan.lines) == 7
        expected = {line.upstream_key: line.weight for line in plan.lines}
        assert all(type(weight) is Decimal for weight in expected.values())
        snapshot_json = plan.lines[0].facts_snapshot
        restored = facts_snapshot_from_dict(snapshot_json)
        assert restored.to_dict() == snapshot_json
        event = db.scalar(select(LedgerEvent).where(LedgerEvent.plan_id == plan_id))
        assert event.type == "PLAN_CREATED"
        assert event.external_id is None
        assert event.payload == {
            "budget_cents": 1000,
            "payable_total_cents": plan.payable_total_cents,
            "reserved_total_cents": plan.reserved_total_cents,
            "line_count": 7,
        }
        db.commit()

    with db_factory() as db:
        persisted = db.get(Plan, plan_id)
        actual = {line.upstream_key: line.weight for line in persisted.lines}
        assert actual == expected
        assert all(type(weight) is Decimal for weight in actual.values())
        assert persisted.est_payout_fees_cents is not None
        assert persisted.charge_cents is None
        assert all(facts_snapshot_from_dict(line.facts_snapshot).to_dict() == line.facts_snapshot for line in persisted.lines)


def test_create_rejects_invalid_budget_unavailable_session_and_empty_eligibility(db_factory):
    with db_factory() as db:
        with pytest.raises(ValidationError) as invalid:
            PlanService.create(db, "missing", True, date(2026, 11, 6))
        assert invalid.value.code == "invalid_budget"
        with pytest.raises(ValidationError):
            PlanService.create(db, "missing", 99, date(2026, 11, 6))
        with pytest.raises(ValidationError):
            PlanService.create(db, "missing", 1_000_001, date(2026, 11, 6))
        with pytest.raises(NotFoundError):
            PlanService.create(db, "missing", 1000, date(2026, 11, 6))

    with db_factory() as db:
        session = add_session(db, ["unmapped"])
        add_unresolved(db, session.id, "unmapped")
        db.commit()
    with db_factory() as db, pytest.raises(AllocationError, match="no eligible"):
        PlanService.create(db, session.id, 1000, date(2026, 11, 6))

    with db_factory() as db:
        session = add_session(db, [], status="resolving")
        db.commit()
    with db_factory() as db, pytest.raises(StateError) as not_ready:
        PlanService.create(db, session.id, 1000, date(2026, 11, 6))
    assert not_ready.value.code == "session_not_ready"


def test_create_rejects_allocation_failure_without_persistence(db_factory, monkeypatch):
    from dividend.services import plan_service

    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    def fail_allocation(*args, **kwargs):
        raise AllocationError("allocation rejected")

    monkeypatch.setattr(plan_service, "allocate", fail_allocation)
    with db_factory() as db:
        with pytest.raises(AllocationError, match="allocation rejected"):
            PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        assert db.scalar(select(func.count()).select_from(Plan)) == 0
        assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 0


def test_create_failure_during_line_or_ledger_insert_rolls_back_plan_writes(db_factory, monkeypatch):
    from dividend.services import plan_service

    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    with db_factory() as db:
        def fail_ledger(*args, **kwargs):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(plan_service, "append_ledger_event", fail_ledger)
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        assert db.scalar(select(func.count()).select_from(Plan)) == 0
        assert db.scalar(select(func.count()).select_from(PlanLine)) == 0
        assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 0

    monkeypatch.undo()
    with db_factory() as db:
        from sqlalchemy import event

        def fail_line_insert(mapper, connection, target):
            raise RuntimeError("line insert unavailable")

        event.listen(PlanLine, "before_insert", fail_line_insert)
        try:
            with pytest.raises(RuntimeError, match="line insert unavailable"):
                PlanService.create(db, session_id, 1000, date(2026, 11, 6))
            assert db.scalar(select(func.count()).select_from(Plan)) == 0
            assert db.scalar(select(func.count()).select_from(PlanLine)) == 0
            assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 0
        finally:
            event.remove(PlanLine, "before_insert", fail_line_insert)


def test_repeated_create_requests_follow_existing_non_idempotent_contract(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    with db_factory() as db:
        first = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        second = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        assert first.id != second.id
        assert db.scalar(select(func.count()).select_from(Plan)) == 2
        assert db.scalar(select(func.count()).select_from(LedgerEvent)) == 4


def test_create_does_not_commit_unrelated_caller_work(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    with db_factory() as db:
        unrelated = Maintainer(
            upstream_key="unrelated/project",
            display_name="Unrelated",
            email="unrelated@example.test",
            opted_in=False,
        )
        db.add(unrelated)
        PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        assert db.in_transaction()
        db.close()  # caller rolls back instead of committing its transaction

    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Plan)) == 0
        assert db.scalar(
            select(func.count()).select_from(Maintainer).where(
                Maintainer.upstream_key == "unrelated/project"
            )
        ) == 0


@pytest.mark.parametrize("new_budget", [900, 1100])
def test_adjust_budget_replaces_lines_and_persists_new_snapshot_allocation(db_factory, new_budget):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id

    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id
        old_ids = {line.id for line in plan.lines}
        old_weights = {line.upstream_key: line.weight for line in plan.lines}

    with db_factory() as db:
        plan = PlanService.adjust_budget(
            db, plan_id, new_budget, date(2026, 11, 6)
        )
        db.flush()
        assert plan.budget_cents == new_budget
        assert plan.est_payout_fees_cents == 25 * sum(line.payable_cents > 0 for line in plan.lines)
        assert plan.charge_cents is None
        assert all(line.id not in old_ids for line in plan.lines)
        assert len(plan.lines) == 7
        assert sum(line.amount_cents for line in plan.lines) == new_budget
        assert sum(line.payable_cents for line in plan.lines) == plan.payable_total_cents
        assert sum(line.reserved_cents for line in plan.lines) == plan.reserved_total_cents
        assert all(line.payable_cents + line.reserved_cents == line.amount_cents for line in plan.lines)
        assert {line.upstream_key: line.weight for line in plan.lines} == old_weights
        assert all(type(line.weight) is Decimal for line in plan.lines)
        assert [line.upstream_key for line in plan.lines] == sorted(line.upstream_key for line in plan.lines)
        assert all(facts_snapshot_from_dict(line.facts_snapshot).to_dict() == line.facts_snapshot for line in plan.lines)
        event = db.scalar(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id,
            LedgerEvent.type == "PLAN_BUDGET_ADJUSTED",
        ))
        assert event.type == "PLAN_BUDGET_ADJUSTED"
        assert event.external_id is None
        assert event.payload == {"old_budget_cents": 1000, "new_budget_cents": new_budget}
        db.commit()

    with db_factory() as db:
        persisted = db.get(Plan, plan_id)
        assert persisted.budget_cents == new_budget
        assert sum(line.amount_cents for line in persisted.lines) == new_budget
        assert all(line.id not in old_ids for line in persisted.lines)


def test_adjust_budget_rejects_missing_non_draft_and_invalid_budget(db_factory):
    with db_factory() as db:
        with pytest.raises(NotFoundError) as missing:
            PlanService.adjust_budget(db, "missing", 1000, date(2026, 11, 6))
        assert missing.value.code == "plan_not_found"

    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        original_ids = {line.id for line in plan.lines}
        plan.state = "funding"
        db.commit()
    with db_factory() as db:
        with pytest.raises(StateError) as non_draft:
            PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
        assert non_draft.value.code == "invalid_plan_transition"
        assert non_draft.value.current_state == "funding"
        assert non_draft.value.allowed_transitions == ["draft"]
        assert db.scalar(select(func.count()).select_from(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id,
            LedgerEvent.type == "PLAN_BUDGET_ADJUSTED",
        )) == 0

    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        plan.state = "draft"
        plan.frozen_at = datetime(2026, 11, 6, tzinfo=UTC)
        db.commit()
    with db_factory() as db:
        with pytest.raises(StateError) as frozen:
            PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
        assert frozen.value.current_state == "draft"
        assert frozen.value.allowed_transitions == []
        plan = db.get(Plan, plan_id)
        plan.frozen_at = None
        db.commit()

    with db_factory() as db:
        for bad_budget in (True, 99, 1_000_001):
            with pytest.raises(ValidationError) as invalid:
                PlanService.adjust_budget(db, plan_id, bad_budget, date(2026, 11, 6))
            assert invalid.value.code == "invalid_budget"
        current = db.get(Plan, plan_id)
        assert current.budget_cents == 1000
        assert {line.id for line in current.lines} == original_ids


def test_adjust_budget_allocation_failure_leaves_plan_and_lines_unchanged(db_factory, monkeypatch):
    from dividend.services import plan_service

    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    def fail_allocation(*args, **kwargs):
        raise AllocationError("adjustment allocation failed")

    monkeypatch.setattr(plan_service, "allocate", fail_allocation)
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        original_ids = {line.id for line in plan.lines}
        with pytest.raises(AllocationError, match="adjustment allocation failed"):
            PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
        assert plan.budget_cents == 1000
        assert {line.id for line in plan.lines} == original_ids
        assert db.scalar(select(func.count()).select_from(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id,
            LedgerEvent.type == "PLAN_BUDGET_ADJUSTED",
        )) == 0


@pytest.mark.parametrize("failure_stage", ["line", "ledger"])
def test_adjust_budget_insert_failures_restore_old_plan_and_lines(db_factory, monkeypatch, failure_stage):
    from dividend.services import plan_service
    from sqlalchemy import event

    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    if failure_stage == "ledger":
        def fail_ledger(*args, **kwargs):
            raise RuntimeError("adjustment ledger unavailable")
        monkeypatch.setattr(plan_service, "append_ledger_event", fail_ledger)
    else:
        def fail_line(mapper, connection, target):
            raise RuntimeError("adjustment line unavailable")
        event.listen(PlanLine, "before_insert", fail_line)

    try:
        with db_factory() as db:
            plan = db.get(Plan, plan_id)
            original_ids = {line.id for line in plan.lines}
            with pytest.raises(RuntimeError, match="adjustment (ledger|line) unavailable"):
                PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
            assert plan.budget_cents == 1000
            assert {line.id for line in plan.lines} == original_ids
            assert db.scalar(select(func.count()).select_from(LedgerEvent).where(
                LedgerEvent.plan_id == plan_id,
                LedgerEvent.type == "PLAN_BUDGET_ADJUSTED",
            )) == 0
    finally:
        monkeypatch.undo()
        if failure_stage == "line":
            event.remove(PlanLine, "before_insert", fail_line)


def test_adjust_budget_does_not_commit_or_discard_unrelated_caller_work(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    with db_factory() as db:
        unrelated = Maintainer(
            upstream_key="adjustment-unrelated/project",
            display_name="Unrelated",
            email="unrelated@example.test",
            opted_in=False,
        )
        db.add(unrelated)
        plan = PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
        assert db.in_transaction()
        assert db.get(Maintainer, unrelated.id) is unrelated
        assert plan.budget_cents == 1100
        db.rollback()

    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Maintainer).where(
            Maintainer.upstream_key == "adjustment-unrelated/project"
        )) == 0
        persisted = db.get(Plan, plan_id)
        assert persisted.budget_cents == 1000


def test_approve_freezes_draft_through_ledger_transition_and_is_idempotent(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    with db_factory() as db:
        approved = PlanService.approve(db, plan_id)
        assert approved.state == "funding"
        assert approved.frozen_at is not None
        assert approved.frozen_at.tzinfo is not None
        assert approved.order_attempt == 1
        assert approved.charge_cents is None
        assert PlanService.approve(db, plan_id).state == "funding"
        events = list(db.scalars(select(LedgerEvent).where(
            LedgerEvent.plan_id == plan_id,
            LedgerEvent.type == "PLAN_APPROVED",
        )))
        assert len(events) == 1
        assert events[0].payload == {
            "charge_cents": approved.charge_cents, "order_attempt": 1,
            "approval_snapshot_sha256": approved.fee_policy_snapshot["approval_proposal"]["sha256"],
        }
        original_hash = approved.fee_policy_snapshot["approval_proposal"]["sha256"]
        with pytest.raises(StateError):
            PlanService.adjust_budget(db, plan_id, 1100, date(2026, 11, 6))
        assert approved.fee_policy_snapshot["approval_proposal"]["sha256"] == original_hash


@pytest.mark.parametrize("tamper", ["snapshot", "hash"])
def test_approval_rejects_proposal_integrity_failure_without_freezing(db_factory, tamper):
    with db_factory.begin() as db:
        session = add_allocation_session(db)
        plan = PlanService.create(db, session.id, 1000, date(2026, 11, 6))
        plan_id = plan.id
        changed = dict(plan.fee_policy_snapshot)
        frozen = dict(changed["approval_proposal"])
        proposal = dict(frozen["snapshot"])
        if tamper == "snapshot":
            projects = list(proposal["projects"])
            projects[0] = {**projects[0], "reserved_cents": projects[0]["reserved_cents"] + 1}
            proposal["projects"] = projects
            frozen["snapshot"] = proposal
        else:
            frozen["sha256"] = "0" * 64
        changed["approval_proposal"] = frozen
        plan.fee_policy_snapshot = changed

    with db_factory() as db, pytest.raises(ValidationError) as error:
        PlanService.approve(db, plan_id)
    assert error.value.code == "approval_snapshot_invalid"
    with db_factory() as db:
        plan = db.get(Plan, plan_id)
        assert (plan.state, plan.frozen_at, plan.order_attempt) == ("draft", None, 0)


def test_approve_does_not_commit_unrelated_caller_work(db_factory):
    with db_factory() as db:
        session = add_allocation_session(db)
        db.commit()
        session_id = session.id
    with db_factory() as db:
        plan = PlanService.create(db, session_id, 1000, date(2026, 11, 6))
        db.commit()
        plan_id = plan.id

    with db_factory() as db:
        unrelated = Maintainer(
            upstream_key="approval-unrelated/project",
            display_name="Unrelated",
            email="unrelated@example.test",
            opted_in=False,
        )
        db.add(unrelated)
        approved = PlanService.approve(db, plan_id)
        assert approved.state == "funding"
        assert unrelated in db.new
        db.close()

    with db_factory() as db:
        assert db.get(Plan, plan_id).state == "funding"
        assert db.scalar(select(func.count()).select_from(Maintainer).where(
            Maintainer.upstream_key == "approval-unrelated/project"
        )) == 0


def test_unverified_legacy_maintainer_is_not_assigned_a_recipient_split(db_factory, tmp_path):
    with db_factory.begin() as db:
        session = add_session(db, ["one-source"])
        add_resolved_project(db, session.id, "one/project", "one-source", opted_in=True)
        from dividend.config import DEFAULT_POLICY_PATH
        policy = tmp_path / "single-project-policy.yaml"
        policy.write_text(DEFAULT_POLICY_PATH.read_text().replace(
            "per_project_cap_pct: 15", "per_project_cap_pct: 100"
        ))
        plan = PlanService.create(db, session.id, 1000, date(2026, 11, 6), policy)
        line = plan.lines[0]
        assert line.amount_cents == 1000  # Project allocation amount remains unchanged.
        assert line.payable_cents == 0
        assert line.reserved_cents == 1000
        assert line.maintainer_splits == []
        assert plan.est_payout_fees_cents == 0


@pytest.mark.parametrize("shares", [(10000,), (6000, 4000), (5000, 3000, 2000)])
def test_configured_shares_and_identity_claims_are_not_contribution_evidence(db_factory, tmp_path, shares):
    with db_factory.begin() as db:
        session = add_session(db, ["many-source"])
        add_resolved_project(db, session.id, "many/project", "many-source", opted_in=True)
        if len(shares) > 1:
            for index, share_bps in enumerate(shares):
                db.add(MaintainerRecipient(
                    id=str(uuid5(NAMESPACE_URL, f"recipient-{len(shares)}-{index}")),
                    upstream_key="many/project", display_name=f"Maintainer {index + 1}",
                    email=f"person-{index + 1}@example.test", opted_in=True,
                    market_code="US", share_bps=share_bps, active=True,
                ))
            db.flush()
        recipients = list(db.scalars(select(Maintainer).where(Maintainer.upstream_key == "many/project")))
        recipients += list(db.scalars(select(MaintainerRecipient).where(MaintainerRecipient.upstream_key == "many/project")))
        for index, recipient in enumerate(recipients, 1):
            db.add(MaintainerClaim(recipient_id=recipient.id, upstream_key="many/project",
                                   status="verified", github_user_id=900000 + index,
                                   github_login=f"maintainer{index}", verified_at=datetime.now(UTC)))
        db.flush()
        from dividend.config import DEFAULT_POLICY_PATH
        policy = tmp_path / f"multi-{len(shares)}-policy.yaml"
        policy.write_text(DEFAULT_POLICY_PATH.read_text().replace(
            "per_project_cap_pct: 15", "per_project_cap_pct: 100"
        ))
        plan = PlanService.create(db, session.id, 1000, date(2026, 11, 6), policy)
        line = plan.lines[0]
        assert line.amount_cents == 1000
        assert line.payable_cents == 0
        assert line.reserved_cents == 1000
        assert line.maintainer_splits == []
        assert plan.est_payout_fees_cents == 0


def test_curl_snapshot_scores_only_explicitly_mapped_server_evidence(db_factory, tmp_path):
    with db_factory.begin() as db:
        session = add_session(db, ["curl-source"])
        add_resolved_project(db, session.id, "github.com/curl/curl", "curl-source", opted_in=True)
        from dividend.config import DEFAULT_POLICY_PATH
        policy = tmp_path / "curl-policy.yaml"
        policy.write_text(DEFAULT_POLICY_PATH.read_text().replace(
            "per_project_cap_pct: 15", "per_project_cap_pct: 100"
        ))
        plan = PlanService.create(db, session.id, 1000, date(2026, 11, 6), policy)
        line = plan.lines[0]
        assert line.amount_cents == 1000
        assert line.payable_cents == 737
        assert line.reserved_cents == 263
        assert sum(row["amount_cents"] for row in line.maintainer_splits) == 737
        assert line.maintainer_splits[0]["share_bps"] == 10000
        assert plan.payable_total_cents == line.payable_cents
        assert plan.reserved_total_cents == line.reserved_cents


def test_largest_remainder_splits_exact_cents_with_stable_id_ties():
    recipients = tuple(MaintainerShare(
        maintainer_id=f"00000000-0000-0000-0000-00000000000{index}",
        display_name=f"Maintainer {index}", email=f"person{index}@example.test",
        share_bps=share,
    ) for index, share in enumerate((3334, 3333, 3333), 1))
    splits = split_cents_by_share(101, recipients)
    assert [item["amount_cents"] for item in splits] == [34, 34, 33]
    assert sum(item["amount_cents"] for item in splits) == 101


def test_eligible_share_total_must_be_one_hundred_percent(db_factory):
    with db_factory() as db:
        session = add_session(db, ["invalid-share"])
        add_resolved_project(db, session.id, "invalid/project", "invalid-share", opted_in=True)
        db.add_all([
            MaintainerRecipient(id=str(uuid5(NAMESPACE_URL, f"bad-{index}")),
                                upstream_key="invalid/project", display_name=f"Person {index}",
                                email=f"person{index}@example.test", opted_in=True,
                                market_code="US", share_bps=share, active=True)
            for index, share in enumerate((6000, 3000))
        ])
        db.flush()
        with pytest.raises(AllocationError, match="exactly 100 percent"):
            PlanService.create(db, session.id, 1000, date(2026, 11, 6))


@pytest.mark.parametrize(("opted_in", "market"), [(False, "US"), (True, "CA"), (True, "US")])
def test_opted_out_ineligible_or_missing_recipient_is_not_paid(db_factory, tmp_path, opted_in, market):
    with db_factory() as db:
        session = add_session(db, ["recipient-source"])
        add_resolved_project(db, session.id, "recipient/project", "recipient-source", opted_in=True)
        db.add(MaintainerRecipient(
            id=str(uuid5(NAMESPACE_URL, f"ineligible-{opted_in}-{market}")),
            upstream_key="recipient/project", display_name="Configured maintainer",
            email="" if market == "US" and opted_in else "person@example.test",
            opted_in=opted_in, market_code=market, share_bps=10000, active=True,
        ))
        db.flush()
        if opted_in and market == "US":
            with pytest.raises(AllocationError, match="invalid recipient details"):
                PlanService.assemble_allocation_inputs(db, session.id, date(2026, 11, 6))
        else:
            from dividend.config import DEFAULT_POLICY_PATH
            policy = tmp_path / "ineligible-policy.yaml"
            policy.write_text(DEFAULT_POLICY_PATH.read_text().replace(
                "per_project_cap_pct: 15", "per_project_cap_pct: 100"
            ))
            plan = PlanService.create(db, session.id, 1000, date(2026, 11, 6), policy)
            line = plan.lines[0]
            assert line.payable_cents == 0
            assert line.reserved_cents == 1000
            assert line.maintainer_splits == []
