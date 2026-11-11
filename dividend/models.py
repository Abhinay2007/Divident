"""SQLAlchemy entities defined by the Dividend LLD."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship
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
    synthetic_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synthetic_fixture_version: Mapped[str | None] = mapped_column(String(64))
    synthetic_policy_version: Mapped[str | None] = mapped_column(String(64))
    synthetic_run_key: Mapped[str | None] = mapped_column(String(64))
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
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="unmapped")
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
    __table_args__ = (
        CheckConstraint("share_bps > 0 AND share_bps <= 10000", name="ck_maintainers_share_bps"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    market_code: Mapped[str | None] = mapped_column(String(2))
    share_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)

    additional_recipients: Mapped[list["MaintainerRecipient"]] = relationship(
        primaryjoin=lambda: Maintainer.upstream_key == foreign(MaintainerRecipient.upstream_key),
        viewonly=True,
    )


class MaintainerRecipient(Base):
    """Additional named payees; Maintainer remains the legacy one-per-project row."""

    __tablename__ = "maintainer_recipients"
    __table_args__ = (
        CheckConstraint("share_bps > 0 AND share_bps <= 10000", name="ck_maintainer_recipients_share_bps"),
        Index("ix_maintainer_recipients_upstream_key", "upstream_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False)
    market_code: Mapped[str | None] = mapped_column(String(2))
    share_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class MaintainerClaim(Base):
    """GitHub identity claim and short-lived, invitation-backed claim session."""

    __tablename__ = "maintainer_claims"
    __table_args__ = (
        UniqueConstraint("recipient_id", name="uq_maintainer_claims_recipient"),
        UniqueConstraint("github_user_id", name="uq_maintainer_claims_github_user"),
        CheckConstraint("status IN ('invited', 'pending', 'verified')", name="ck_maintainer_claims_status"),
        CheckConstraint("github_user_id IS NULL OR github_user_id > 0", name="ck_maintainer_claims_github_id"),
        Index("ix_maintainer_claims_invitation_hash", "invitation_token_hash", unique=True),
        Index("ix_maintainer_claims_session_hash", "session_token_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    recipient_id: Mapped[str] = mapped_column(String(36), nullable=False)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="invited")
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    github_login: Mapped[str | None] = mapped_column(String(39))
    invitation_token_hash: Mapped[str | None] = mapped_column(String(64))
    invitation_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    session_token_hash: Mapped[str | None] = mapped_column(String(64))
    session_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    oauth_state_hash: Mapped[str | None] = mapped_column(String(64))
    oauth_code_verifier: Mapped[str | None] = mapped_column(String(128))
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_session_id", "session_id"), Index("ix_plans_state", "state"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    synthetic_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synthetic_fixture_version: Mapped[str | None] = mapped_column(String(64))
    synthetic_policy_version: Mapped[str | None] = mapped_column(String(64))
    budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    payable_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    est_payout_fees_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    est_incoming_fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charge_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_policy_version: Mapped[str | None] = mapped_column(String(64))
    fee_policy_snapshot: Mapped[dict | None] = mapped_column(JSON)
    order_id: Mapped[str | None] = mapped_column(String(64))
    approval_url: Mapped[str | None] = mapped_column(String(512))
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
    # Frozen recipient identities and integer-cent splits for this plan line.
    maintainer_splits: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

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
    estimated_fees_cents: Mapped[int | None] = mapped_column(Integer)
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
    # Polymorphic stable ID: legacy Maintainer or MaintainerRecipient.
    maintainer_id: Mapped[str | None] = mapped_column(String(36))
    sender_item_id: Mapped[str] = mapped_column(String(63), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(256), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    external_id: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    batch: Mapped["PayoutBatch"] = relationship(back_populates="items")
    plan: Mapped["Plan"] = relationship(back_populates="payout_items")


class PaymentOperation(Base):
    """Durable identity and outcome for one external payment operation attempt."""

    __tablename__ = "payment_operations"
    __table_args__ = (
        CheckConstraint("kind IN ('order', 'capture', 'payout')", name="ck_payment_operations_kind"),
        CheckConstraint("attempt > 0", name="ck_payment_operations_attempt_positive"),
        CheckConstraint(
            "state IN ('intent', 'submitting', 'confirmed', 'rejected', 'unknown', 'resolved', 'cancelled')",
            name="ck_payment_operations_state",
        ),
        CheckConstraint(
            "(kind = 'payout' AND payout_batch_id IS NOT NULL) OR "
            "(kind IN ('order', 'capture') AND payout_batch_id IS NULL)",
            name="ck_payment_operations_batch_kind",
        ),
        CheckConstraint(
            "kind = 'payout' OR prior_plan_state IS NULL",
            name="ck_payment_operations_prior_state_kind",
        ),
        CheckConstraint(
            "prior_plan_state IS NULL OR prior_plan_state IN ('funded', 'settled_with_exceptions')",
            name="ck_payment_operations_prior_state",
        ),
        UniqueConstraint("plan_id", "kind", "attempt", name="uq_payment_operations_plan_kind_attempt"),
        UniqueConstraint("request_id", name="uq_payment_operations_request_id"),
        UniqueConstraint("payout_batch_id", name="uq_payment_operations_payout_batch"),
        Index(
            "uq_payment_operations_active_kind", "plan_id", "kind", unique=True,
            sqlite_where=text("state IN ('intent', 'submitting', 'unknown')"),
            postgresql_where=text("state IN ('intent', 'submitting', 'unknown')"),
        ),
        Index("ix_payment_operations_plan_kind", "plan_id", "kind", "attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    synthetic_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    payout_batch_id: Mapped[str | None] = mapped_column(ForeignKey("payout_batches.id"))
    provider_result: Mapped[dict | None] = mapped_column(JSON)
    prior_plan_state: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SyntheticSandboxRecipient(Base):
    """Sandbox destination kept separate from opted-in maintainer records."""

    __tablename__ = "synthetic_sandbox_recipients"
    __table_args__ = (UniqueConstraint("upstream_key", "config_sha256", name="uq_synthetic_recipient_config"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    upstream_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    market_code: Mapped[str] = mapped_column(String(2), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


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
