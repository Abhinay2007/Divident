"""Deterministic provider for local fake mode and payment tests."""

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from ..errors import PaymentProviderError
from .base import (BatchDetail, BatchResult, CaptureResult, ItemDetail, OrderDetail,
                   OrderResult, PayoutItemInput)


class FakeProvider:
    def __init__(self, *, outcomes=None, duplicate_batch_error=False,
                 fees_per_item_cents: int | None = None, order_fee_pct: Decimal | None = None,
                 order_fee_fixed_cents: int | None = None, order_error=None,
                 capture_error=None, payout_error=None):
        self.outcomes = outcomes or {}
        self.duplicate_batch_error = duplicate_batch_error
        self.fees_per_item_cents = fees_per_item_cents
        self.order_fee_pct = order_fee_pct
        self.order_fee_fixed_cents = order_fee_fixed_cents
        self.order_error, self.capture_error, self.payout_error = order_error, capture_error, payout_error
        self.orders = {}
        self.request_orders = {}
        self.captures = {}
        self.capture_orders = {}
        self.batches = {}
        self.request_batches = {}

    def create_order(self, plan_id, amount_cents, currency, request_id, return_url, cancel_url):
        if self.order_error:
            raise self.order_error
        if request_id in self.request_orders:
            return self.request_orders[request_id]
        result = OrderResult(str(uuid4()), f"{return_url}&order=fake")
        self.orders[result.order_id] = (plan_id, amount_cents, currency)
        self.request_orders[request_id] = result
        return result

    def capture_order(self, order_id, request_id):
        if self.capture_error:
            raise self.capture_error
        if request_id in self.captures:
            return self.captures[request_id]
        _plan_id, gross, currency = self.orders[order_id]
        variable = (Decimal(gross) * self.order_fee_pct / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP) if self.order_fee_pct is not None else Decimal(0)
        fee = int(variable) + (self.order_fee_fixed_cents or 0)
        result = CaptureResult(str(uuid4()), gross, fee, gross - fee, currency, "COMPLETED")
        self.captures[request_id] = result
        self.capture_orders[request_id] = order_id
        return result

    def get_order(self, order_id):
        captures = [result for request_id, result in self.captures.items()
                    if self.capture_orders[request_id] == order_id]
        return OrderDetail(order_id, captures[:1], "US")

    def create_payout_batch(self, sender_batch_id, request_id, items):
        if self.payout_error:
            raise self.payout_error
        if sender_batch_id in self.batches:
            if self.duplicate_batch_error:
                raise PaymentProviderError("Batch with given sender_batch_id already exists",
                                           code="USER_BUSINESS_ERROR")
            return self.batches[sender_batch_id][0]
        batch_id = str(uuid4())
        fee = self.fees_per_item_cents * len(items) if self.fees_per_item_cents is not None else None
        details = [ItemDetail(item.sender_item_id, str(uuid4()),
                              self.outcomes.get(item.sender_item_id, "SUCCESS").upper(), item.amount_cents)
                   for item in items]
        batch = BatchResult(batch_id, "SUCCESS", fee)
        self.batches[sender_batch_id] = (batch, BatchDetail(batch_id, "SUCCESS", fee, details))
        self.request_batches[request_id] = sender_batch_id
        return batch

    def get_payout_batch(self, batch_id):
        return next(detail for _result, detail in self.batches.values() if detail.batch_id == batch_id)
