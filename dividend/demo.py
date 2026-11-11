"""Deterministic fixture-backed demo database seeding."""

from pathlib import Path
import re
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, sessionmaker

from .db import seed_if_empty
from .enrichment.fixtures import FixtureEnricher
from .models import Maintainer, MaintainerRecipient, Session
from .services.session_service import SessionService
from .upstream.overrides import load_overrides

_DATA = Path(__file__).resolve().parent.parent / "data"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _load_sandbox_recipient_config(path: Path | str | None) -> dict:
    """Parse legacy single-recipient schema 2 or explicit multi-recipient schema 3."""
    if path is None:
        return {"schema": 2, "recipients": {}}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if type(raw) is not dict or set(raw) != {"schema", "recipients"}:
        raise ValueError("sandbox recipient file must contain only schema and recipients")
    recipients = raw["recipients"]
    if raw["schema"] == 2:
        if type(recipients) is not dict or any(
            type(key) is not str or type(recipient) is not dict
            or set(recipient) != {"email", "market_code"}
            or type(recipient["email"]) is not str or not _EMAIL.fullmatch(recipient["email"])
            or recipient["market_code"] != "US" for key, recipient in recipients.items()
        ):
            raise ValueError("Schema 2 recipients require a valid email and market_code: US")
        return {"schema": 2, "recipients": recipients}
    if raw["schema"] != 3 or type(recipients) is not dict:
        raise ValueError("sandbox recipient schema must be 2 or 3")

    required = {"id", "display_name", "email", "opted_in", "market_code", "share_bps", "active"}
    seen_ids = set()
    for key, rows in recipients.items():
        if type(key) is not str or not key or type(rows) is not list or not rows:
            raise ValueError("Schema 3 recipients must map upstream keys to nonempty maintainer lists")
        eligible_shares = 0
        for row in rows:
            if type(row) is not dict or set(row) != required:
                raise ValueError("Schema 3 maintainer fields are invalid")
            try:
                maintainer_id = str(UUID(row["id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Schema 3 maintainer IDs must be UUIDs") from exc
            if maintainer_id != row["id"] or maintainer_id in seen_ids:
                raise ValueError("Schema 3 maintainer IDs must be canonical and unique")
            seen_ids.add(maintainer_id)
            if (type(row["display_name"]) is not str or not row["display_name"].strip()
                    or type(row["email"]) is not str or not _EMAIL.fullmatch(row["email"])
                    or type(row["opted_in"]) is not bool or type(row["active"]) is not bool
                    or type(row["market_code"]) is not str
                    or not re.fullmatch(r"[A-Z]{2}", row["market_code"])
                    or type(row["share_bps"]) is not int
                    or not 1 <= row["share_bps"] <= 10000):
                raise ValueError("Schema 3 maintainer details are invalid")
            if row["opted_in"] is True and row["active"] is True and row["market_code"] == "US":
                eligible_shares += row["share_bps"]
        if eligible_shares and eligible_shares != 10000:
            raise ValueError("Eligible Schema 3 shares must total exactly 10000 basis points")
    return {"schema": 3, "recipients": recipients}


def load_sandbox_recipients(path: Path | str | None) -> dict[str, dict[str, str]]:
    """Compatibility loader for the existing one-recipient schema 2 format."""
    config = _load_sandbox_recipient_config(path)
    if config["schema"] != 2:
        raise ValueError("Schema 3 is a multi-maintainer format and cannot be read as schema 2")
    return config["recipients"]


def _validate_demo_recipient_keys(config: dict, fixture_by_key: dict[str, dict]) -> None:
    opted_in_keys = {key for key, row in fixture_by_key.items() if row.get("opted_in") is True}
    if set(config["recipients"]) - opted_in_keys:
        raise ValueError("sandbox recipients must match opted-in demo maintainers")


def _apply_multi_recipients(db: DbSession, config: dict,
                            fixture_by_key: dict[str, dict]) -> None:
    _validate_demo_recipient_keys(config, fixture_by_key)
    incoming = config["recipients"]
    existing_rows = list(db.scalars(select(MaintainerRecipient).where(
        MaintainerRecipient.upstream_key.in_(incoming)
    ))) if incoming else []
    incoming_ids = {row["id"] for rows in incoming.values() for row in rows}
    id_rows = list(db.scalars(select(MaintainerRecipient).where(
        MaintainerRecipient.id.in_(incoming_ids)
    ))) if incoming_ids else []
    legacy_id_rows = list(db.scalars(select(Maintainer).where(Maintainer.id.in_(incoming_ids)))) if incoming_ids else []
    if legacy_id_rows:
        raise ValueError("Schema 3 maintainer IDs must not collide with legacy recipient IDs")
    existing_by_key: dict[str, dict[str, MaintainerRecipient]] = {}
    existing_by_id = {}
    for row in {row.id: row for row in [*existing_rows, *id_rows]}.values():
        if row.upstream_key in incoming:
            existing_by_key.setdefault(row.upstream_key, {})[row.id] = row
        existing_by_id[row.id] = row
    for key, rows in incoming.items():
        supplied_ids = {row["id"] for row in rows}
        if set(existing_by_key.get(key, {})) - supplied_ids:
            raise ValueError("Schema 3 must include existing maintainer IDs; mark removed recipients inactive")
        for row in rows:
            existing = existing_by_id.get(row["id"])
            if existing is not None and existing.upstream_key != key:
                raise ValueError("Maintainer IDs cannot be reassigned to another upstream project")
        final = {**existing_by_key.get(key, {})}
        for row in rows:
            final[row["id"]] = row
        eligible = [row for row in final.values() if (
            (row["opted_in"] if isinstance(row, dict) else row.opted_in) is True
            and (row["active"] if isinstance(row, dict) else row.active) is True
            and (row["market_code"] if isinstance(row, dict) else row.market_code) == "US"
        )]
        if eligible and sum(row["share_bps"] if isinstance(row, dict) else row.share_bps
                            for row in eligible) != 10000:
            raise ValueError("Eligible maintainer shares must total exactly 10000 basis points")
    for key, rows in incoming.items():
        for values in rows:
            recipient = existing_by_id.get(values["id"])
            if recipient is None:
                recipient = MaintainerRecipient(upstream_key=key, **values)
                db.add(recipient)
                existing_by_id[values["id"]] = recipient
            else:
                for field, value in values.items():
                    setattr(recipient, field, value)


def configure_sandbox_recipients(
    session_factory: sessionmaker[DbSession], path: Path | str,
) -> int:
    """Apply only explicitly configured recipients to matching demo maintainers."""
    config = _load_sandbox_recipient_config(path)
    fixture = _load_demo_maintainers()
    fixture_by_key = {row["upstream_key"]: row for row in fixture}
    _validate_demo_recipient_keys(config, fixture_by_key)
    recipients = config["recipients"]

    if config["schema"] == 3:
        with session_factory.begin() as db:
            _apply_multi_recipients(db, config, fixture_by_key)
        return sum(len(rows) for rows in recipients.values())

    with session_factory.begin() as db:
        if config["schema"] == 2:
            configured_multi = db.scalar(select(MaintainerRecipient.id).where(
                MaintainerRecipient.upstream_key.in_(recipients)
            ).limit(1)) if recipients else None
            if configured_multi is not None:
                raise ValueError("Use schema 3 to update a project with multiple recipients")
        existing = {
            row.upstream_key: row
            for row in db.scalars(select(Maintainer).where(
                Maintainer.upstream_key.in_(recipients)
            ))
        }
        for key, recipient in recipients.items():
            maintainer = existing.get(key)
            if maintainer is None:
                maintainer = Maintainer(
                    upstream_key=key,
                    display_name=fixture_by_key[key]["display_name"],
                    email=recipient["email"],
                    opted_in=True,
                    market_code="US",
                )
                db.add(maintainer)
            else:
                maintainer.email = recipient["email"]
                maintainer.opted_in = True
                maintainer.market_code = "US"
                maintainer.active = True
                maintainer.share_bps = 10000
    return len(recipients)


def _load_demo_maintainers() -> list[dict]:
    fixture = yaml.safe_load((_DATA / "demo_maintainers.yaml").read_text(encoding="utf-8"))
    if (type(fixture) is not dict or fixture.get("schema") != 1
            or fixture.get("identities_are_synthetic") is not True
            or type(fixture.get("maintainers")) is not list):
        raise ValueError("invalid synthetic demo maintainer fixture")
    maintainers = fixture["maintainers"]
    keys = [row.get("upstream_key") for row in maintainers if type(row) is dict]
    if len(keys) != len(maintainers) or len(keys) != len(set(keys)):
        raise ValueError("demo maintainers must have unique upstream keys")
    return maintainers


def seed_demo(db: DbSession, sandbox_recipients: dict | None = None) -> Session:
    """Create one resolved demo session and its synthetic maintainer records."""
    maintainers = _load_demo_maintainers()
    config = sandbox_recipients or {"schema": 2, "recipients": {}}
    if "schema" not in config:
        config = {"schema": 2, "recipients": config}
    fixture_by_key = {row["upstream_key"]: row for row in maintainers}
    _validate_demo_recipient_keys(config, fixture_by_key)
    legacy_recipients = config["recipients"] if config["schema"] == 2 else {}
    multi_recipients = config if config["schema"] == 3 else None

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
            email=legacy_recipients.get(row["upstream_key"], {}).get("email", row["email"]),
            opted_in=row["opted_in"],
            market_code=legacy_recipients.get(row["upstream_key"], {}).get("market_code"),
        )
        for row in maintainers
    ])
    db.flush()
    if multi_recipients is not None:
        _apply_multi_recipients(db, multi_recipients,
                                {row["upstream_key"]: row for row in maintainers})
    return session


def seed_demo_if_empty(
    session_factory: sessionmaker[DbSession],
    sandbox_recipients_file: Path | str | None = None,
) -> bool:
    """Seed only an empty database, preserving existing application rows."""
    return seed_if_empty(
        session_factory,
        lambda db: seed_demo(db, _load_sandbox_recipient_config(sandbox_recipients_file)),
    )
