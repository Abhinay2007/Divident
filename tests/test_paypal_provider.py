import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import httpx
import pytest
from paypal.core import HttpRequest, HttpResponse, JsonBody

from dividend.errors import PaymentProviderError
from dividend.payments.base import PayoutItemInput
from dividend.payments.paypal import PayPalProvider
from dividend.payments.paypal.client import SANDBOX_URL


@dataclass
class StubResponse:
    status_code: int
    chunks: list[bytes]
    headers: Mapping[str, str] = field(default_factory=lambda: {"content-type": "application/json"})
    url: str = f"{SANDBOX_URL}/mock"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


class StubOrdersTransport:
    def __init__(self, *responses: StubResponse):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def response(status: int, body: dict) -> StubResponse:
    return StubResponse(status, [json.dumps(body).encode()])


def token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "sandbox-token", "token_type": "Bearer",
                                     "expires_in": 3600})


def provider(orders: list[StubResponse], payout_handler=None):
    payout_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payout_requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        if payout_handler is None:
            return httpx.Response(500)
        return payout_handler(request)

    http = httpx.Client(base_url=SANDBOX_URL, transport=httpx.MockTransport(handle))
    sdk_transport = StubOrdersTransport(*orders)
    adapter = PayPalProvider("client-id", "client-secret", http_client=http,
                             orders_transport=sdk_transport)
    return adapter, sdk_transport, payout_requests


def amount(cents: str, currency: str = "USD") -> dict:
    return {"currency_code": currency, "value": cents}


def order(capture: dict | None = None) -> dict:
    unit = {"reference_id": "plan", "amount": amount("3.50")}
    if capture is not None:
        unit["payments"] = {"captures": [capture]}
    return {"id": "ORDER-1", "status": "COMPLETED", "purchase_units": [unit]}


def test_create_order_uses_sandbox_oauth_stable_request_id_and_integer_cents():
    adapter, transport, auth_requests = provider([response(201, {
        "id": "ORDER-1", "status": "CREATED",
        "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}],
    })])
    try:
        result = adapter.create_order("plan-1", 1234, "USD", "plan:plan-1:order:1",
                                      "https://app.test/return", "https://app.test/cancel")
        assert (result.order_id, result.approval_url) == (
            "ORDER-1", "https://www.sandbox.paypal.com/approve")
        request = transport.requests[0]
        assert request.url.startswith(SANDBOX_URL)
        assert request.headers["paypal-request-id"] == "plan:plan-1:order:1"
        assert isinstance(request.body, JsonBody)
        assert request.body.value["purchase_units"][0]["amount"] == {
            "currency_code": "USD", "value": "12.34"}
        assert auth_requests[0].url == f"{SANDBOX_URL}/v1/oauth2/token"
        assert auth_requests[0].headers["authorization"].startswith("Basic ")
    finally:
        adapter.close()


def test_capture_and_get_order_preserve_observed_breakdown_and_unknown_fees():
    captured = {"id": "CAPTURE-1", "status": "COMPLETED",
                "seller_receivable_breakdown": {
                    "gross_amount": amount("10.00"), "net_amount": amount("9.16"),
                    "paypal_fee": amount("0.84"),
                }}
    no_fee = {"id": "CAPTURE-2", "status": "COMPLETED",
              "seller_receivable_breakdown": {
                  "gross_amount": amount("1.00"), "net_amount": amount("0.48"),
              }}
    adapter, transport, _ = provider([response(201, order(captured)), response(200, order(no_fee))])
    try:
        result = adapter.capture_order("ORDER-1", "plan:plan-1:capture:1")
        assert (result.capture_id, result.gross_cents, result.fee_cents,
                result.net_cents, result.currency, result.status) == (
                    "CAPTURE-1", 1000, 84, 916, "USD", "COMPLETED")
        request = transport.requests[0]
        assert request.headers["paypal-request-id"] == "plan:plan-1:capture:1"
        assert request.headers["prefer"] == "return=representation"
        recovered = adapter.get_order("ORDER-1").captures[0]
        assert recovered.fee_cents is None
        assert recovered.gross_cents - recovered.net_cents == 52
    finally:
        adapter.close()


def test_capture_rejects_non_usd_or_unparseable_money():
    invalid = {"id": "CAPTURE-1", "status": "COMPLETED",
               "seller_receivable_breakdown": {
                   "gross_amount": amount("1.00", "EUR"), "net_amount": amount("1.00"),
               }}
    adapter, _, _ = provider([response(201, order(invalid))])
    try:
        with pytest.raises(PaymentProviderError) as error:
            adapter.capture_order("ORDER-1", "request-1")
        assert error.value.code == "invalid_capture_response"
        assert error.value.detail["outcome_unknown"] is True
    finally:
        adapter.close()


def test_payout_request_uses_stable_identifiers_and_raw_provider_status():
    def payout_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sandbox-token"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["sender_batch_header"]["sender_batch_id"] == "plan-1-payout-1"
            assert body["items"][0]["amount"] == {"value": "3.00", "currency": "USD"}
            return httpx.Response(201, json={"batch_header": {
                "payout_batch_id": "BATCH-1", "batch_status": "PENDING"}})
        return httpx.Response(200, json={
            "batch_header": {"payout_batch_id": "BATCH-1", "batch_status": "SUCCESS",
                             "fees": {"value": "0.50", "currency": "USD"}},
            "items": [{"payout_item": {"sender_item_id": "item-1",
                                         "amount": {"value": "3.00", "currency": "USD"}},
                       "payout_item_id": "ITEM-1", "transaction_status": "SUCCESS"}],
        })

    adapter, _, requests = provider([], payout_handler)
    try:
        batch = adapter.create_payout_batch(
            "plan-1-payout-1", "plan:plan-1:payout:1",
            [PayoutItemInput("item-1", "person@example.test", 300)],
        )
        assert (batch.batch_id, batch.status, batch.fees_cents) == ("BATCH-1", "PENDING", None)
        assert requests[-1].headers["paypal-request-id"] == "plan:plan-1:payout:1"
        detail = adapter.get_payout_batch("BATCH-1")
        assert (detail.batch_id, detail.status, detail.fees_cents) == ("BATCH-1", "SUCCESS", 50)
        assert [(item.sender_item_id, item.external_id, item.raw_status, item.amount_cents)
                for item in detail.items] == [("item-1", "ITEM-1", "SUCCESS", 300)]
    finally:
        adapter.close()


def test_duplicate_sender_batch_rejection_is_preserved_without_a_retry():
    def payout_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={"name": "USER_BUSINESS_ERROR",
                                             "message": "Batch with given sender_batch_id already exists"})
        raise AssertionError("duplicate payout must not trigger another request")

    adapter, _, requests = provider([], payout_handler)
    try:
        with pytest.raises(PaymentProviderError) as error:
            adapter.create_payout_batch("same-id", "request-1",
                                        [PayoutItemInput("item-1", "a@example.test", 100)])
        assert error.value.code == "USER_BUSINESS_ERROR"
        assert error.value.detail["outcome_unknown"] is True
        assert sum(request.url.path == "/v1/payments/payouts" for request in requests) == 1
    finally:
        adapter.close()


def test_payout_read_timeout_is_uncertain_and_not_retried():
    calls = 0

    def payout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method == "POST":
            raise httpx.ReadTimeout("response lost", request=request)
        raise AssertionError("uncertain payout must not be retried")

    adapter, _, _ = provider([], payout_handler)
    try:
        with pytest.raises(PaymentProviderError) as error:
            adapter.create_payout_batch("batch-1", "request-1",
                                        [PayoutItemInput("item-1", "a@example.test", 100)])
        assert error.value.detail["outcome_unknown"] is True
        assert calls == 1
    finally:
        adapter.close()
