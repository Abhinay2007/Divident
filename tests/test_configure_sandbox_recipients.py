from datetime import date
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import delete, func, select

from dividend.cli import main
from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.demo import seed_demo_if_empty
from dividend.models import (
    LedgerEvent, Maintainer, MaintainerRecipient, Plan, PlanLine, Session,
    SourceResolution, UpstreamProject,
)
from dividend.services.plan_service import PlanService


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "populated.db"
    engine = create_database_engine(f"sqlite:///{path}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    assert seed_demo_if_empty(factory)
    yield engine, factory, f"sqlite:///{path}"
    engine.dispose()


def recipient_file(tmp_path, recipients):
    path = tmp_path / "recipients.yaml"
    path.write_text(yaml.safe_dump({"schema": 2, "recipients": recipients}), encoding="utf-8")
    return path


def multi_recipient_file(tmp_path, recipients):
    path = tmp_path / "multi-recipients.yaml"
    path.write_text(yaml.safe_dump({"schema": 3, "recipients": recipients}), encoding="utf-8")
    return path


def multi_recipient(recipient_id, name, email, share_bps, *, opted_in=True, market="US", active=True):
    return {"id": recipient_id, "display_name": name, "email": email,
            "opted_in": opted_in, "market_code": market,
            "share_bps": share_bps, "active": active}


def run_import(database_url, path):
    return main([
        "configure-sandbox-recipients", "--database-url", database_url,
        "--file", str(path),
    ])


def counts(factory):
    with factory() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (Session, SourceResolution, UpstreamProject, Plan, PlanLine, LedgerEvent)
        }


def test_valid_import_updates_and_inserts_only_configured_recipients(database, tmp_path, capsys):
    _engine, factory, database_url = database
    with factory.begin() as db:
        existing = db.scalar(select(Maintainer).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        ))
        existing_id = existing.id
        db.execute(delete(Maintainer).where(
            Maintainer.upstream_key == "salsa.debian.org/apt-team/apt"
        ))
    before = counts(factory)
    email = "sandbox-recipient@example.test"
    path = recipient_file(tmp_path, {
        "github.com/curl/curl": {"email": email, "market_code": "US"},
        "salsa.debian.org/apt-team/apt": {
            "email": "another-sandbox@example.test", "market_code": "US",
        },
    })

    assert run_import(database_url, path) == 0
    captured = capsys.readouterr()
    assert captured.out == "2\n"
    assert captured.err == ""
    assert email not in captured.out + captured.err

    with factory() as db:
        curl = db.scalar(select(Maintainer).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        ))
        apt = db.scalar(select(Maintainer).where(
            Maintainer.upstream_key == "salsa.debian.org/apt-team/apt"
        ))
        assert (curl.id, curl.email, curl.opted_in, curl.market_code) == (
            existing_id, email, True, "US",
        )
        assert (apt.email, apt.opted_in, apt.market_code) == (
            "another-sandbox@example.test", True, "US",
        )
    assert counts(factory) == before

    assert run_import(database_url, path) == 0
    assert capsys.readouterr().out == "2\n"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Maintainer)) == 13
        assert db.scalar(select(Maintainer.id).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        )) == existing_id


def test_requested_python_module_command_runs_and_prints_only_count(database, tmp_path):
    _engine, _factory, database_url = database
    path = recipient_file(tmp_path, {
        "github.com/curl/curl": {"email": "sandbox-recipient@example.test", "market_code": "US"},
    })
    result = subprocess.run(
        [sys.executable, "-m", "dividend.cli", "configure-sandbox-recipients",
         "--database-url", database_url, "--file", str(path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "1\n"
    assert result.stderr == ""
    assert "sandbox-recipient@example.test" not in result.stdout + result.stderr


@pytest.mark.parametrize(("raw", "email"), [
    ("schema: 2\nrecipients:\n  github.com/unknown/project:\n    email: valid@example.test\n    market_code: US\n", "valid@example.test"),
    ("schema: 2\nrecipients:\n  github.com/openssl/openssl:\n    email: valid@example.test\n    market_code: US\n", "valid@example.test"),
    ("schema: 2\nrecipients:\n  github.com/curl/curl:\n    email: not-an-email\n    market_code: US\n", "not-an-email"),
    ("schema: 2\nrecipients:\n  github.com/curl/curl:\n    email: valid@example.test\n    market_code: CA\n", "valid@example.test"),
    ("schema: 2\nrecipients: {}\nextra: true\n", ""),
    ("schema: [\nrecipients: {", ""),
])
def test_invalid_recipient_files_are_rejected_without_partial_changes(
    database, tmp_path, capsys, raw, email,
):
    _engine, factory, database_url = database
    before_counts = counts(factory)
    with factory() as db:
        before = db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        ))
    path = tmp_path / "invalid.yaml"
    path.write_text(raw, encoding="utf-8")

    assert run_import(database_url, path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to configure sandbox recipients." in captured.err
    if email:
        assert email not in captured.err
    assert counts(factory) == before_counts
    with factory() as db:
        assert db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        )) == before


def test_invalid_entry_after_valid_entry_cannot_partially_update_database(
    database, tmp_path, capsys,
):
    _engine, factory, database_url = database
    with factory() as db:
        before = db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        ))
    path = recipient_file(tmp_path, {
        "github.com/curl/curl": {"email": "sandbox@example.test", "market_code": "US"},
        "github.com/unknown/project": {"email": "other@example.test", "market_code": "US"},
    })

    assert run_import(database_url, path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sandbox@example.test" not in captured.err
    with factory() as db:
        assert db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        )) == before


def test_database_failure_rolls_back_all_recipient_changes(database, tmp_path, capsys):
    engine, factory, database_url = database
    with factory() as db:
        before = db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        ))
    with engine.begin() as connection:
        connection.execute(delete(Maintainer.__table__).where(
            Maintainer.__table__.c.upstream_key == "salsa.debian.org/apt-team/apt"
        ))
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_apt_recipient BEFORE INSERT ON maintainers "
            "WHEN NEW.upstream_key = 'salsa.debian.org/apt-team/apt' "
            "BEGIN SELECT RAISE(ABORT, 'invalid recipient row'); END"
        )
    path = recipient_file(tmp_path, {
        "github.com/curl/curl": {"email": "sandbox@example.test", "market_code": "US"},
        "salsa.debian.org/apt-team/apt": {"email": "apt@example.test", "market_code": "US"},
    })

    assert run_import(database_url, path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sandbox@example.test" not in captured.err
    with factory() as db:
        assert db.scalar(select(Maintainer.email).where(
            Maintainer.upstream_key == "github.com/curl/curl"
        )) == before
        assert db.scalar(select(Maintainer.id).where(
            Maintainer.upstream_key == "salsa.debian.org/apt-team/apt"
        )) is None
        assert db.scalar(select(func.count()).select_from(Session)) == 1


def test_import_leaves_existing_plans_projects_and_sessions_unchanged(
    database, tmp_path,
):
    _engine, factory, database_url = database
    with factory.begin() as db:
        session = db.scalar(select(Session).where(Session.mode == "demo"))
        plan = PlanService.create(db, session.id, 10_000, date(2026, 11, 6))
        plan_id = plan.id
    before_counts = counts(factory)
    with factory() as db:
        before_lines = db.execute(select(
            PlanLine.upstream_key, PlanLine.amount_cents, PlanLine.payable_cents,
            PlanLine.reserved_cents,
        ).where(PlanLine.plan_id == plan_id).order_by(PlanLine.upstream_key)).all()
    path = recipient_file(tmp_path, {
        "github.com/curl/curl": {"email": "sandbox@example.test", "market_code": "US"},
    })

    assert run_import(database_url, path) == 0
    assert counts(factory) == before_counts
    with factory() as db:
        after_lines = db.execute(select(
            PlanLine.upstream_key, PlanLine.amount_cents, PlanLine.payable_cents,
            PlanLine.reserved_cents,
        ).where(PlanLine.plan_id == plan_id).order_by(PlanLine.upstream_key)).all()
        assert db.get(Plan, plan_id).state == "draft"
    assert after_lines == before_lines


def test_schema3_import_adds_multiple_stable_maintainers_idempotently(database, tmp_path, capsys):
    _engine, factory, database_url = database
    first_id, second_id = str(uuid4()), str(uuid4())
    first_email, second_email = "sandbox-one@example.test", "sandbox-two@example.test"
    path = multi_recipient_file(tmp_path, {"github.com/curl/curl": [
        multi_recipient(first_id, "Maintainer One", first_email, 5000),
        multi_recipient(second_id, "Maintainer Two", second_email, 5000),
    ]})

    assert run_import(database_url, path) == 0
    assert capsys.readouterr().out == "2\n"
    assert run_import(database_url, path) == 0
    output = capsys.readouterr()
    assert output.out == "2\n"
    assert first_email not in output.out + output.err
    assert second_email not in output.out + output.err
    with factory() as db:
        rows = list(db.scalars(select(MaintainerRecipient).where(
            MaintainerRecipient.upstream_key == "github.com/curl/curl"
        ).order_by(MaintainerRecipient.id)))
        assert {row.id for row in rows} == {first_id, second_id}
        assert sum(row.share_bps for row in rows if row.opted_in and row.active and row.market_code == "US") == 10000
        assert db.scalar(select(func.count()).select_from(Maintainer)) == 13


@pytest.mark.parametrize("rows", [
    [multi_recipient("not-a-uuid", "One", "valid@example.test", 10000)],
    [multi_recipient(str(uuid4()), "One", "", 10000)],
    [multi_recipient(str(uuid4()), "One", "valid@example.test", 6000),
     multi_recipient(str(uuid4()), "Two", "other@example.test", 3000)],
])
def test_schema3_rejects_invalid_ids_missing_recipient_or_unbalanced_shares(
    database, tmp_path, capsys, rows,
):
    _engine, factory, database_url = database
    with factory() as db:
        before = db.scalar(select(func.count()).select_from(MaintainerRecipient))
    path = multi_recipient_file(tmp_path, {"github.com/curl/curl": rows})
    assert run_import(database_url, path) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "@" not in output.err
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(MaintainerRecipient)) == before


def test_schema3_rejects_duplicate_ids_and_opted_out_fixture_project(database, tmp_path, capsys):
    _engine, factory, database_url = database
    shared_id = str(uuid4())
    row = multi_recipient(shared_id, "One", "sandbox@example.test", 5000)
    duplicate = multi_recipient(shared_id, "Two", "second@example.test", 5000)
    duplicate_path = multi_recipient_file(tmp_path, {"github.com/curl/curl": [row, duplicate]})
    assert run_import(database_url, duplicate_path) == 2
    assert capsys.readouterr().out == ""

    opted_out = multi_recipient_file(tmp_path, {"github.com/openssl/openssl": [
        multi_recipient(str(uuid4()), "Sandbox Maintainer", "sandbox@example.test", 10000),
    ]})
    assert run_import(database_url, opted_out) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "sandbox@example.test" not in output.err
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(MaintainerRecipient)) == 0


def test_schema3_ineligible_and_opted_out_records_can_be_configured_but_never_opt_in(database, tmp_path, capsys):
    _engine, factory, database_url = database
    ineligible = multi_recipient_file(tmp_path, {"github.com/curl/curl": [
        multi_recipient(str(uuid4()), "Canadian Recipient", "sandbox@example.test", 10000, market="CA"),
    ]})
    assert run_import(database_url, ineligible) == 0
    assert capsys.readouterr().out == "1\n"
    with factory() as db:
        row = db.scalar(select(MaintainerRecipient).where(
            MaintainerRecipient.upstream_key == "github.com/curl/curl"
        ))
        assert row.opted_in is True and row.market_code == "CA"

