"""Deterministic fixture-backed demo database seeding."""

from pathlib import Path
import re

import yaml
from sqlalchemy.orm import Session as DbSession, sessionmaker

from .db import seed_if_empty
from .enrichment.fixtures import FixtureEnricher
from .models import Maintainer, Session
from .services.session_service import SessionService
from .upstream.overrides import load_overrides

_DATA = Path(__file__).resolve().parent.parent / "data"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def load_sandbox_recipients(path: Path | str | None) -> dict[str, str]:
    """Load optional, user-supplied sandbox recipients separately from demo identities."""
    if path is None:
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if (type(raw) is not dict or raw.get("schema") != 1
            or type(raw.get("recipients")) is not dict):
        raise ValueError("sandbox recipient file must contain schema: 1 and a recipients mapping")
    recipients = raw["recipients"]
    if any(type(key) is not str or type(email) is not str or not _EMAIL.fullmatch(email)
           for key, email in recipients.items()):
        raise ValueError("sandbox recipients must map upstream keys to valid email addresses")
    return recipients


def seed_demo(db: DbSession, sandbox_recipients: dict[str, str] | None = None) -> Session:
    """Create one resolved demo session and its synthetic maintainer records."""
    fixture = yaml.safe_load((_DATA / "demo_maintainers.yaml").read_text(encoding="utf-8"))
    if (type(fixture) is not dict or fixture.get("schema") != 1
            or fixture.get("identities_are_synthetic") is not True
            or type(fixture.get("maintainers")) is not list):
        raise ValueError("invalid synthetic demo maintainer fixture")

    maintainers = fixture["maintainers"]
    keys = [row.get("upstream_key") for row in maintainers if type(row) is dict]
    if len(keys) != len(maintainers) or len(keys) != len(set(keys)):
        raise ValueError("demo maintainers must have unique upstream keys")
    opted_in_keys = {row["upstream_key"] for row in maintainers if row.get("opted_in") is True}
    sandbox_recipients = sandbox_recipients or {}
    if set(sandbox_recipients) - opted_in_keys:
        raise ValueError("sandbox recipients may only replace opted-in demo maintainers")

    session = SessionService.create(db, "demo", None)
    SessionService.resolve_sources(
        db, session.id,
        FixtureEnricher(_DATA / "demo_enrichment.json"),
        load_overrides(),
    )
    db.add_all([
        Maintainer(
            upstream_key=row["upstream_key"],
            display_name=row["display_name"],
            email=sandbox_recipients.get(row["upstream_key"], row["email"]),
            opted_in=row["opted_in"],
        )
        for row in maintainers
    ])
    db.flush()
    return session


def seed_demo_if_empty(
    session_factory: sessionmaker[DbSession],
    sandbox_recipients_file: Path | str | None = None,
) -> bool:
    """Seed only an empty database, preserving existing application rows."""
    return seed_if_empty(
        session_factory,
        lambda db: seed_demo(db, load_sandbox_recipients(sandbox_recipients_file)),
    )
