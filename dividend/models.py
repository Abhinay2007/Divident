"""SQLAlchemy entities defined by the Dividend LLD."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC timestamps and restore timezone awareness on every dialect."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ExactDecimalText(TypeDecorator[Decimal]):
    """Persist finite Decimal values as exact, exponent-preserving text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if type(value) is not Decimal or not value.is_finite():
            raise TypeError("weight must be a finite Decimal")
        return str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError("persisted weight must be finite")
        return result


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_status", "status"), Index("ix_sessions_created_at", "created_at"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    source_names: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # NULL only for legacy sessions or paste intake without binary membership.
    source_binaries: Mapped[dict[str, list[str]] | None] = mapped_column(JSON, nullable=True)
    binary_count: Mapped[int | None] = mapped_column(Integer)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    source_resolutions: Mapped[list["SourceResolution"]] = relationship(back_populates="session")
    upstream_projects: Mapped[list["UpstreamProject"]] = relationship(back_populates="session")
    plans: Mapped[list["Plan"]] = relationship(back_populates="session")


class SourceResolution(Base):
    __tablename__ = "source_resolutions"
    __table_args__ = (UniqueConstraint("session_id", "source_name", name="uq_source_resolutions_session_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    upstream_key: Mapped[str | None] = mapped_column(String(256))
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[list[dict] | None] = mapped_column(JSON)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    session: Mapped["Session"] = relationship(back_populates="source_resolutions")


class UpstreamProject(Base):
    __tablename__ = "upstream_projects"
    __table_args__ = (UniqueConstraint("session_id", "upstream_key", name="uq_upstream_projects_session_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_url: Mapped[str | None] = mapped_column(String(512))
    binary_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_names: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Per-source original FactSet payloads; nullable only for pre-upgrade rows.
    source_facts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    need_signals: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    session: Mapped["Session"] = relationship(back_populates="upstream_projects")


class Maintainer(Base):
    __tablename__ = "maintainers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_session_id", "session_id"), Index("ix_plans_state", "state"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    payable_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    est_payout_fees_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charge_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64))
    order_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    capture_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payout_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    capture_gross_cents: Mapped[int | None] = mapped_column(Integer)
    capture_fee_cents: Mapped[int | None] = mapped_column(Integer)
    capture_net_cents: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    session: Mapped["Session"] = relationship(back_populates="plans")
    lines: Mapped[list["PlanLine"]] = relationship(
        back_populates="plan", order_by="PlanLine.upstream_key"
    )
    payout_batches: Mapped[list["PayoutBatch"]] = relationship(back_populates="plan")
    payout_items: Mapped[list["PayoutItem"]] = relationship(back_populates="plan")
    ledger_events: Mapped[list["LedgerEvent"]] = relationship(back_populates="plan")


class PlanLine(Base):
    __tablename__ = "plan_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    weight: Mapped[Decimal] = mapped_column(ExactDecimalText(), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    payable_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable only for legacy rows created before immutable snapshots existed.
    facts_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    plan: Mapped["Plan"] = relationship(back_populates="lines")


class PayoutBatch(Base):
    __tablename__ = "payout_batches"
    __table_args__ = (UniqueConstraint("plan_id", "attempt", name="uq_payout_batches_plan_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    sender_batch_id: Mapped[str] = mapped_column(String(256), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="intent")
    fees_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    plan: Mapped["Plan"] = relationship(back_populates="payout_batches")
    items: Mapped[list["PayoutItem"]] = relationship(back_populates="batch")


class PayoutItem(Base):
    __tablename__ = "payout_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "sender_item_id", name="uq_payout_items_batch_sender_item"),
        Index("ix_payout_items_plan_id", "plan_id"),
        Index("ix_payout_items_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("payout_batches.id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    sender_item_id: Mapped[str] = mapped_column(String(63), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(256), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    external_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    batch: Mapped["PayoutBatch"] = relationship(back_populates="items")
    plan: Mapped["Plan"] = relationship(back_populates="payout_items")


class LedgerEvent(Base):
    __tablename__ = "ledger_events"
    __table_args__ = (
        Index("uq_ledger_events_type_external_id", "type", "external_id", unique=True, sqlite_where=text("external_id IS NOT NULL"), postgresql_where=text("external_id IS NOT NULL")),
        Index("ix_ledger_events_plan_created_at", "plan_id", "created_at"),
        Index("ix_ledger_events_session_created_at", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"))
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    plan: Mapped["Plan | None"] = relationship(back_populates="ledger_events")


class LLMCache(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
