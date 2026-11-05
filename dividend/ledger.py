"""Append-only ledger operations and guarded plan transitions."""

import base64
import binascii
import builtins
import csv
import io
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .errors import StateError, ValidationError
from .models import LedgerEvent, Plan

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"draft", "funding", "cancelled", "failed"},
    "funding": {"cancelled", "funded", "failed"},
    "funded": {"paying", "failed"},
    "paying": {"settled", "settled_with_exceptions", "failed"},
    "settled_with_exceptions": {"paying", "failed"},
    "settled": {"failed"},
    "cancelled": {"failed"},
    "failed": {"failed"},
}
_CSV_FIELDS = ("id", "created_at", "type", "plan_id", "session_id", "external_id", "payload")


def _event(db: DbSession, event_type: str, payload: dict, *, plan_id=None, session_id=None, external_id=None) -> LedgerEvent:
    event = LedgerEvent(type=event_type, payload=payload, plan_id=plan_id, session_id=session_id, external_id=external_id)
    db.add(event)
    return event


def append(db: DbSession, type: str, payload: dict, plan_id=None, session_id=None, external_id=None) -> LedgerEvent:
    """Append an event, reusing an existing non-null idempotency key if present."""
    if external_id is not None:
        existing = db.scalar(select(LedgerEvent).where(LedgerEvent.type == type, LedgerEvent.external_id == external_id))
        if existing is not None:
            logger.debug("Ledger event deduplicated", extra={"event_type": type, "external_id": external_id})
            return existing
    if external_id is None:
        event = _event(db, type, payload, plan_id=plan_id, session_id=session_id, external_id=external_id)
        db.flush()
        return event
    # Flush caller-owned work before the collision handler: begin_nested() also
    # flushes unconditionally, and failures here are not ledger insert failures.
    db.flush()
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        # sqlite3's legacy transaction mode does not BEGIN for SELECT; without
        # an explicit outer BEGIN, releasing a SAVEPOINT may commit it.
        connection.exec_driver_sql("BEGIN")
    try:
        with db.begin_nested():
            event = _event(db, type, payload, plan_id=plan_id, session_id=session_id, external_id=external_id)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(LedgerEvent).where(LedgerEvent.type == type, LedgerEvent.external_id == external_id))
        if existing is None:
            raise
        logger.debug("Ledger event deduplicated after uniqueness race", extra={"event_type": type, "external_id": external_id})
        return existing
    return event


def _allowed(from_state: str) -> list[str]:
    return sorted(_ALLOWED_TRANSITIONS.get(from_state, set()))


def transition_plan(db: DbSession, plan_id: str, from_state: str, to_state: str, event_type: str, payload: dict, external_id=None, *, additional_events=None) -> None:
    """Guard and commit a plan transition with all corresponding ledger events."""
    allowed = _allowed(from_state)
    if to_state not in allowed:
        raise StateError("Plan transition is not allowed", code="invalid_plan_transition", current_state=from_state, allowed_transitions=allowed)
    try:
        result = db.execute(update(Plan).where(Plan.id == plan_id, Plan.state == from_state).values(state=to_state))
        if result.rowcount == 0:
            current_state = db.scalar(select(Plan.state).where(Plan.id == plan_id))
            raise StateError("Plan state changed before transition", code="stale_plan_state", current_state=current_state, allowed_transitions=_allowed(current_state) if current_state is not None else [])
        session_id = db.scalar(select(Plan.session_id).where(Plan.id == plan_id))
        _event(db, event_type, payload, plan_id=plan_id, session_id=session_id, external_id=external_id)
        db.flush()
        for entry in additional_events or ():
            if not isinstance(entry, Mapping) or "type" not in entry or "payload" not in entry:
                raise ValueError("additional event entries require type and payload")
            _event(db, entry["type"], entry["payload"], plan_id=plan_id, session_id=session_id, external_id=entry.get("external_id"))
            db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        if not isinstance(cursor, str) or not cursor or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cursor):
            raise ValueError
        unpadded = cursor.rstrip("=")
        expected_padding = (-len(unpadded)) % 4
        supplied_padding = len(cursor) - len(unpadded)
        if len(unpadded) % 4 == 1 or (supplied_padding and supplied_padding != expected_padding):
            raise ValueError
        raw = base64.b64decode(unpadded + "=" * expected_padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != unpadded:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"v", "created_at", "id"}:
            raise ValueError
        if type(data["v"]) is not int or data["v"] != 1:
            raise ValueError
        if not isinstance(data["created_at"], str) or not isinstance(data["id"], str) or not data["id"]:
            raise ValueError
        created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return created_at.astimezone(UTC), data["id"]
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error, json.JSONDecodeError):
        raise ValidationError("Invalid or unsupported ledger cursor", code="invalid_ledger_cursor") from None


def _encode_cursor(event: LedgerEvent) -> str:
    payload = json.dumps({"v": 1, "created_at": event.created_at.astimezone(UTC).isoformat(), "id": event.id}, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _filtered_query(plan_id=None, session_id=None, types=None):
    query = select(LedgerEvent)
    if plan_id is not None:
        query = query.where(LedgerEvent.plan_id == plan_id)
    if session_id is not None:
        query = query.where(LedgerEvent.session_id == session_id)
    if types is not None:
        query = query.where(LedgerEvent.type.in_(types))
    return query


def list(db: DbSession, plan_id=None, session_id=None, types=None, cursor=None, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("limit must be a positive integer", code="invalid_ledger_limit")
    limit = min(limit, 500)
    query = _filtered_query(plan_id, session_id, types)
    if cursor is not None:
        created_at, event_id = _decode_cursor(cursor)
        query = query.where(or_(LedgerEvent.created_at > created_at, and_(LedgerEvent.created_at == created_at, LedgerEvent.id > event_id)))
    rows = builtins.list(db.scalars(query.order_by(LedgerEvent.created_at.asc(), LedgerEvent.id.asc()).limit(limit + 1)))
    has_more = len(rows) > limit
    events = rows[:limit]
    return events, _encode_cursor(events[-1]) if has_more and events else None


def export_csv(db: DbSession, plan_id=None, session_id=None) -> Iterator[str]:
    writer_buffer = io.StringIO(newline="")
    writer = csv.writer(writer_buffer, lineterminator="\r\n")
    writer.writerow(_CSV_FIELDS)
    yield writer_buffer.getvalue()
    result = db.scalars(
        _filtered_query(plan_id, session_id)
        .order_by(LedgerEvent.created_at.asc(), LedgerEvent.id.asc())
        .execution_options(yield_per=100, stream_results=True)
    )
    for event in result:
        writer_buffer.seek(0)
        writer_buffer.truncate(0)
        timestamp = event.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        writer.writerow([event.id, timestamp, event.type, event.plan_id, event.session_id, event.external_id, json.dumps(event.payload, separators=(",", ":"), sort_keys=True)])
        yield writer_buffer.getvalue()


class LedgerService:
    append = staticmethod(append)
    transition_plan = staticmethod(transition_plan)
    list = staticmethod(list)
    export_csv = staticmethod(export_csv)
