"""Sandbox PayPal adapter for Orders v2 and Payouts v1."""

from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx
import httpx2
from paypal import PaypalClient
from paypal.core import (ApiError, ClientCredentials, HttpClient, OAuthProviderError,
                         OAuthToken, RawError, UNSET)
from paypal.models import Error

from ...errors import PaymentProviderError, ProviderTimeout
from ..base import (BatchDetail, BatchResult, CaptureResult, ItemDetail,
                    OrderDetail, OrderResult, PayoutItemInput)

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
_UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
_SDK_UNSENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError)


def _cents(money: Any) -> int | None:
    if money is None or money is UNSET or money.currency_code != "USD":
        return None
    try:
        value = Decimal(money.value) * 100
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


def _money(amount_cents: int) -> str:
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


def _capture(capture: Any) -> CaptureResult:
    breakdown = capture.seller_receivable_breakdown
    if breakdown is UNSET:
        raise PaymentProviderError("PayPal capture has no fee breakdown", code="invalid_capture_response",
                                   retry_hint="reconcile", detail={"outcome_unknown": True})
    gross = _cents(breakdown.gross_amount)
    net = _cents(breakdown.net_amount)
    fee = None if breakdown.paypal_fee is UNSET else _cents(breakdown.paypal_fee)
    capture_id = capture.id
    status = capture.status
    if gross is None or net is None or fee is None and breakdown.paypal_fee is not UNSET:
        raise PaymentProviderError("PayPal capture amounts are missing or invalid", code="invalid_capture_response",
                                   retry_hint="reconcile", detail={"outcome_unknown": True})
    if capture_id is UNSET or not capture_id or status is UNSET:
        raise PaymentProviderError("PayPal capture identity or status is missing", code="invalid_capture_response",
                                   retry_hint="reconcile", detail={"outcome_unknown": True})
    return CaptureResult(capture_id, gross, fee, net, "USD", str(status))


def _captures(order: Any) -> list[CaptureResult]:
    purchase_units = order.purchase_units
    if purchase_units is UNSET:
        return []
    found: list[CaptureResult] = []
    for unit in purchase_units:
        if unit.payments is UNSET or unit.payments.captures is UNSET:
            continue
        found.extend(_capture(capture) for capture in unit.payments.captures)
    return found


def _payer_country_code(order: Any) -> str | None:
    payer = order.payer
    if payer is UNSET or payer is None or payer.address is UNSET or payer.address is None:
        return None
    country = payer.address.country_code
    return country if isinstance(country, str) else None


def _provider_code(error: object) -> str:
    if isinstance(error, Error):
        details = error.details
        if not isinstance(details, list):
            details = []
        for detail in details:
            if detail.issue:
                return detail.issue
        return error.name
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return f"http_{error.status_code}"
        name = body.get("name") if isinstance(body, dict) else None
        if type(name) is str:
            return name
        return f"http_{error.status_code}"
    return "provider_error"


def _sdk_failure(exc: Exception, *, write: bool) -> PaymentProviderError:
    if isinstance(exc, PaymentProviderError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        code = _provider_code(exc.error)
        unknown = write and status >= 500
        if isinstance(exc.error, OAuthProviderError):
            unknown = False
        if code in {"ORDER_ALREADY_CAPTURED", "TRANSACTION_ALREADY_COMPLETED"}:
            unknown = False
        return PaymentProviderError("PayPal rejected the Orders request", code=code,
                                    retry_hint="reconcile" if unknown else None,
                                    detail={"outcome_unknown": unknown, "status_code": status})
    if isinstance(exc, _SDK_UNSENT):
        return PaymentProviderError("PayPal connection failed before sending the request",
                                    code="provider_unavailable", detail={"outcome_unknown": False})
    if isinstance(exc, httpx2.RequestError):
        return ProviderTimeout("PayPal Orders outcome is unknown", code="provider_outcome_unknown",
                               retry_hint="reconcile", detail={"outcome_unknown": True})
    if isinstance(exc, (ValueError, TypeError)):
        return PaymentProviderError("PayPal returned an invalid Orders response",
                                    code="invalid_provider_response",
                                    retry_hint="reconcile" if write else None,
                                    detail={"outcome_unknown": write})
    return PaymentProviderError("PayPal Orders request failed", code="provider_error",
                                retry_hint="reconcile" if write else None,
                                detail={"outcome_unknown": write})


class _SandboxTokenSource:
    """One cached OAuth token shared by the SDK and Payouts HTTP calls."""

    def __init__(self, http: httpx.Client):
        self._http = http
        self._lock = threading.Lock()
        self._token: OAuthToken | None = None
        self._expires_at: float | None = None

    def fetch(self, credentials: ClientCredentials) -> OAuthToken:
        with self._lock:
            if self._token is not None and (self._expires_at is None or time.monotonic() < self._expires_at):
                return self._token
            try:
                response = self._http.post(
                    "/v1/oauth2/token",
                    auth=(credentials.client_id, credentials.client_secret),
                    data={"grant_type": "client_credentials"},
                )
                response.raise_for_status()
                token = OAuthToken.model_validate(response.json())
            except httpx.HTTPStatusError as exc:
                raise PaymentProviderError("PayPal Sandbox authentication was rejected",
                                           code="paypal_auth_error",
                                           detail={"outcome_unknown": False,
                                                   "status_code": exc.response.status_code}) from exc
            except (httpx.RequestError, ValueError, TypeError) as exc:
                raise PaymentProviderError("PayPal Sandbox authentication failed",
                                           code="paypal_auth_error",
                                           detail={"outcome_unknown": False}) from exc
            self._token = token
            self._expires_at = time.monotonic() + token.expires_in if token.expires_in is not None else None
            return token


class PayPalProvider:
    """Synchronous provider. The app holds one instance for its lifespan."""

    def __init__(self, client_id: str, client_secret: str, *,
                 http_client: httpx.Client | None = None, orders_transport: HttpClient | None = None):
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(base_url=SANDBOX_URL, timeout=30.0)
        self._credentials = ClientCredentials(client_id=client_id, client_secret=client_secret)
        self._token_source = _SandboxTokenSource(self._http)
        self._orders = PaypalClient(
            base_url=SANDBOX_URL,
            timeout=30.0,
            retry_options=0,
            oauth2=self._credentials,
            oauth2_token_source=self._token_source,
            custom_http_client=orders_transport,
        )

    def create_order(self, plan_id: str, amount_cents: int, currency: str,
                     request_id: str, return_url: str, cancel_url: str) -> OrderResult:
        if currency != "USD" or type(amount_cents) is not int or amount_cents <= 0:
            raise PaymentProviderError("PayPal Orders require a positive USD amount", code="invalid_payment_amount")
        try:
            order = self._orders.orders.create_order({
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": plan_id,
                    "custom_id": plan_id,
                    "amount": {"currency_code": "USD", "value": _money(amount_cents)},
                }],
                "application_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            }, pay_pal_request_id=request_id)
        except Exception as exc:
            raise _sdk_failure(exc, write=True) from exc
        links = order.links
        if not isinstance(links, list):
            links = []
        approval_url = next((link.href for link in links if link.rel in {"approve", "payer-action"}), None)
        if order.id is UNSET or not order.id or not approval_url:
            raise PaymentProviderError("PayPal order response is incomplete", code="invalid_order_response",
                                       retry_hint="reconcile", detail={"outcome_unknown": True})
        return OrderResult(order.id, approval_url)

    def capture_order(self, order_id: str, request_id: str) -> CaptureResult:
        try:
            order = self._orders.orders.capture_order(order_id, pay_pal_request_id=request_id,
                                                       prefer="return=representation", body={})
            captures = _captures(order)
        except Exception as exc:
            raise _sdk_failure(exc, write=True) from exc
        if len(captures) != 1:
            raise PaymentProviderError("PayPal capture response must contain one capture",
                                       code="invalid_capture_response", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        return captures[0]

    def get_order(self, order_id: str) -> OrderDetail:
        try:
            order = self._orders.orders.get_order(order_id)
            return OrderDetail(order_id, _captures(order), _payer_country_code(order))
        except Exception as exc:
            raise _sdk_failure(exc, write=False) from exc

    def create_payout_batch(self, sender_batch_id: str, request_id: str,
                            items: list[PayoutItemInput]) -> BatchResult:
        if not items or any(item.currency != "USD" or type(item.amount_cents) is not int
                            or item.amount_cents <= 0 for item in items):
            raise PaymentProviderError("PayPal Payouts require positive USD items", code="invalid_payout_amount")
        token = self._token_source.fetch(self._credentials)
        payload = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": "Dividend Sandbox funding",
                "email_message": "A Dividend maintainer payout is ready.",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": item.recipient_email,
                "amount": {"value": _money(item.amount_cents), "currency": item.currency},
                "sender_item_id": item.sender_item_id,
                "note": "Dividend maintainer allocation",
            } for item in items],
        }
        data = self._request("POST", "/v1/payments/payouts", token.access_token,
                             request_id=request_id, payload=payload, write=True)
        header = data.get("batch_header")
        if not isinstance(header, dict):
            raise PaymentProviderError("PayPal payout response has no batch header",
                                       code="invalid_payout_response", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        batch_id = header.get("payout_batch_id")
        status = header.get("batch_status")
        if not isinstance(batch_id, str) or not batch_id or not isinstance(status, str):
            raise PaymentProviderError("PayPal payout response is incomplete",
                                       code="invalid_payout_response", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        return BatchResult(batch_id, status, _payout_fee(header.get("fees")))

    def get_payout_batch(self, batch_id: str) -> BatchDetail:
        token = self._token_source.fetch(self._credentials)
        data = self._request("GET", f"/v1/payments/payouts/{quote(batch_id, safe='')}",
                             token.access_token, write=False)
        header = data.get("batch_header")
        if not isinstance(header, dict):
            raise PaymentProviderError("PayPal payout response has no batch header", code="invalid_payout_response")
        actual_id = header.get("payout_batch_id")
        status = header.get("batch_status")
        raw_items = data.get("items", [])
        if not isinstance(actual_id, str) or not actual_id or not isinstance(status, str) or not isinstance(raw_items, list):
            raise PaymentProviderError("PayPal payout response is incomplete", code="invalid_payout_response")
        items = []
        for entry in raw_items:
            payout_item = entry.get("payout_item") if isinstance(entry, dict) else None
            if not isinstance(payout_item, dict):
                raise PaymentProviderError("PayPal payout item response is incomplete", code="invalid_payout_response")
            sender_item_id = payout_item.get("sender_item_id")
            amount = payout_item.get("amount")
            raw_status = entry.get("transaction_status")
            external_id = entry.get("payout_item_id")
            cents = _payout_money(amount)
            if not isinstance(sender_item_id, str) or not isinstance(raw_status, str) or cents is None:
                raise PaymentProviderError("PayPal payout item response is invalid", code="invalid_payout_response")
            if external_id is not None and not isinstance(external_id, str):
                raise PaymentProviderError("PayPal payout item ID is invalid", code="invalid_payout_response")
            errors = entry.get("errors")
            reason = None
            if isinstance(errors, dict):
                parts = [errors.get("message")]
                details = errors.get("details")
                if isinstance(details, list):
                    parts.extend(detail.get("description") or detail.get("issue")
                                 for detail in details if isinstance(detail, dict))
                reason = "; ".join(part.strip() for part in parts
                                    if isinstance(part, str) and part.strip())[:512] or None
            items.append(ItemDetail(sender_item_id, external_id, raw_status, cents, reason,
                                    amount.get("currency")))
        return BatchDetail(actual_id, status, _payout_fee(header.get("fees")), items)

    def _request(self, method: str, path: str, access_token: str, *, request_id: str | None = None,
                 payload: dict[str, Any] | None = None, write: bool) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if request_id is not None:
            headers["PayPal-Request-Id"] = request_id
        try:
            response = self._http.request(method, path, json=payload, headers=headers)
        except _UNSENT as exc:
            raise PaymentProviderError("PayPal connection failed before sending the request",
                                       code="provider_unavailable", detail={"outcome_unknown": False}) from exc
        except httpx.RequestError as exc:
            raise ProviderTimeout("PayPal Payouts outcome is unknown", code="provider_outcome_unknown",
                                  retry_hint="reconcile", detail={"outcome_unknown": write}) from exc
        if response.is_error:
            code = _http_error_code(response)
            unknown = write and response.status_code >= 500
            if code == "USER_BUSINESS_ERROR":
                unknown = True
            raise PaymentProviderError("PayPal rejected the Payouts request", code=code,
                                       retry_hint="reconcile" if unknown else None,
                                       detail={"outcome_unknown": unknown, "status_code": response.status_code})
        try:
            body = response.json()
        except ValueError as exc:
            raise PaymentProviderError("PayPal returned invalid Payouts JSON", code="invalid_provider_response",
                                       retry_hint="reconcile" if write else None,
                                       detail={"outcome_unknown": write}) from exc
        if not isinstance(body, dict):
            raise PaymentProviderError("PayPal returned an invalid Payouts response", code="invalid_provider_response",
                                       retry_hint="reconcile" if write else None,
                                       detail={"outcome_unknown": write})
        return body

    def close(self) -> None:
        self._orders.close()
        if self._owns_http:
            self._http.close()


def _payout_money(value: object) -> int | None:
    if not isinstance(value, dict) or value.get("currency") != "USD":
        return None
    try:
        cents = Decimal(value["value"]) * 100
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    if not cents.is_finite() or cents != cents.to_integral_value():
        return None
    return int(cents)


def _payout_fee(value: object) -> int | None:
    return _payout_money(value)


def _http_error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}"
    if isinstance(body, dict):
        name = body.get("name")
        if type(name) is str:
            return name
        details = body.get("details")
        if isinstance(details, list):
            for item in details:
                issue = item.get("issue") if isinstance(item, dict) else None
                if type(issue) is str:
                    return issue
    return f"http_{response.status_code}"
