"""Synchronous provider contract shared by fakes and the T8 adapter."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    approval_url: str


@dataclass(frozen=True, slots=True)
class CaptureResult:
    capture_id: str
    gross_cents: int
    fee_cents: int | None
    net_cents: int
    currency: str
    status: str


@dataclass(frozen=True, slots=True)
class OrderDetail:
    order_id: str
    captures: list[CaptureResult]
    payer_country_code: str | None = None


@dataclass(frozen=True, slots=True)
class PayoutItemInput:
    sender_item_id: str
    recipient_email: str
    amount_cents: int
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    status: str
    fees_cents: int | None


@dataclass(frozen=True, slots=True)
class ItemDetail:
    sender_item_id: str
    external_id: str | None
    raw_status: str
    amount_cents: int
    failure_reason: str | None = None
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class BatchDetail:
    batch_id: str
    status: str
    fees_cents: int | None
    items: list[ItemDetail]


class PaymentsProvider(Protocol):
    def create_order(self, plan_id: str, amount_cents: int, currency: str,
                     request_id: str, return_url: str, cancel_url: str) -> OrderResult: ...
    def capture_order(self, order_id: str, request_id: str) -> CaptureResult: ...
    def get_order(self, order_id: str) -> OrderDetail: ...
    def create_payout_batch(self, sender_batch_id: str, request_id: str,
                            items: list[PayoutItemInput]) -> BatchResult: ...
    def get_payout_batch(self, batch_id: str) -> BatchDetail: ...
