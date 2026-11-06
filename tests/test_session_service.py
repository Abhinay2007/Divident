import pytest
from sqlalchemy import func, select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import NotFoundError, StateError, ValidationError
from dividend.models import (
    LedgerEvent, Maintainer, Session, SourceResolution, UpstreamProject,
)
from dividend.services import SessionService


@pytest.fixture
def db_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def scan_payload():
    return {
        "schema": 1,
        "origin": "test-fixture",
        "source_count": 2,
        "binary_count": 3,
        "sources": {"alpha-src": ["alpha-bin", "alpha-tools"], "beta-src": ["beta-bin"]},
    }


def test_create_upload_preserves_membership_and_ledger_atomically(db_factory):
    with db_factory() as db:
        session = SessionService.create(db, "upload", scan_payload())
        session_id = session.id
        assert session.status == "created"
        assert session.source_names == ["alpha-src", "beta-src"]
        assert session.source_binaries == scan_payload()["sources"]
        assert session.source_count == 2
        assert session.binary_count == 3
        db.flush()
        assert db.scalar(select(func.count()).select_from(SourceResolution).where(
            SourceResolution.session_id == session_id
        )) == 2
        event = db.scalar(select(LedgerEvent).where(LedgerEvent.session_id == session_id))
        assert event.type == "SESSION_CREATED"
        assert event.payload == {"mode": "upload", "source_count": 2, "binary_count": 3}
        db.commit()
    with db_factory() as db:
        persisted = SessionService.get(db, session_id)
        assert persisted.source_binaries == scan_payload()["sources"]


def test_create_paste_preserves_source_names_and_marks_binary_membership_unknown(db_factory):
    with db_factory() as db:
        session = SessionService.create(db, "paste", "z-source\na-source\n")
        assert session.source_names == ["a-source", "z-source"]
        assert session.source_binaries is None
        assert session.binary_count is None
        assert session.source_count == 2


def test_create_demo_uses_bundled_scanner_fixture_membership(db_factory):
    with db_factory() as db:
        session = SessionService.create(db, "demo", None)
        assert session.source_binaries
        assert session.source_count == len(session.source_binaries)
        assert session.binary_count == sum(map(len, session.source_binaries.values()))


@pytest.mark.parametrize("payload", [
    {**scan_payload(), "source_count": 1},
    {**scan_payload(), "binary_count": 4},
    {**scan_payload(), "sources": {"alpha-src": ["same-bin"], "beta-src": ["same-bin"]}},
])
def test_create_rejects_aggregate_mismatch_and_duplicate_membership(db_factory, payload):
    with db_factory() as db, pytest.raises(ValidationError) as invalid:
        SessionService.create(db, "upload", payload)
    assert invalid.value.code == "invalid_session_request"


@pytest.mark.parametrize("payload", ["", "bad_name", "dupe\ndupe", "x\n" * 5001])
def test_create_rejects_invalid_paste_source_names(db_factory, payload):
    with db_factory() as db, pytest.raises(ValidationError):
        SessionService.create(db, "paste", payload)


def test_session_lookup_and_state_progress_failure_contract(db_factory):
    with db_factory() as db:
        with pytest.raises(NotFoundError):
            SessionService.get(db, "missing")
        session = SessionService.create(db, "paste", "pkg-a\npkg-b")
        session_id = session.id
        db.commit()

    with db_factory() as db:
        session = SessionService.mark_resolving(db, session_id)
        assert session.status == "resolving"
        assert session.progress_done == 0
        assert session.progress_total == 2
        assert SessionService.mark_resolving(db, session_id).status == "resolving"
        session = SessionService.update_progress(
            db, session_id, stage="mapping", progress_done=1, progress_total=2
        )
        assert (session.stage, session.progress_done, session.progress_total) == ("mapping", 1, 2)
        with pytest.raises(ValidationError):
            SessionService.update_progress(
                db, session_id, stage="mapping", progress_done=0, progress_total=2
            )
        session = SessionService.mark_failed(db, session_id, "fixture resolver unavailable")
        assert session.status == "failed"
        assert session.error_message == "fixture resolver unavailable"
        assert SessionService.mark_failed(db, session_id, "fixture resolver unavailable").status == "failed"
        with pytest.raises(StateError):
            SessionService.mark_resolving(db, session_id)


def test_get_projects_uses_maintainer_opt_in_and_does_not_guess_no_upstream(db_factory):
    with db_factory() as db:
        session = SessionService.create(db, "upload", scan_payload())
        session_id = session.id
        resolutions = list(db.scalars(select(SourceResolution).where(SourceResolution.session_id == session_id)))
        resolutions[0].method = "override"
        resolutions[0].upstream_key = "owner/repo"
        resolutions[1].method = "none"
        project = UpstreamProject(
            session_id=session_id,
            upstream_key="owner/repo",
            display_name="Repo",
            repository_url="https://github.com/owner/repo",
            binary_count=2,
            source_names=[resolutions[0].source_name],
            source_facts={resolutions[0].source_name: {
                "source_name": resolutions[0].source_name,
                "repository_url": "https://github.com/owner/repo",
                "homepage": None,
                "donation": None,
                "github": None,
                "partial": True,
            }},
            need_signals={"archived": None, "last_commit": None, "committers_12m": None,
                           "funding_yml": None, "partial": True},
        )
        db.add(project)
        db.add(Maintainer(
            upstream_key="owner/repo", display_name="Maintainer",
            email="maintainer@example.test", opted_in=True,
        ))
        db.flush()
        result = SessionService.get_projects(db, session_id)
        assert [item.upstream_key for item in result.projects] == ["owner/repo"]
        assert result.projects[0].payability == "payable"
        assert result.unmapped == [resolutions[1].source_name]
        assert result.no_upstream == []
