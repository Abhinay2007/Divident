#!/usr/bin/env python3
"""Explicit, sandbox-only PayPal experiments for Dividend's T1 spike.

Each subcommand performs one operation (plus OAuth token acquisition). The
script never retries calls. Credentials are read from PAYPAL_CLIENT_ID and
PAYPAL_CLIENT_SECRET.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

SANDBOX = "https://api-m.sandbox.paypal.com"
SANDBOX_HOST = "api-m.sandbox.paypal.com"


class SpikeError(Exception):
    pass


def endpoint(value: str | None) -> str:
    """Validate override endpoints so experiments cannot target live PayPal."""
    url = value or SANDBOX
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SpikeError("PayPal endpoint must be an HTTPS URL")
    if parsed.hostname.lower() != SANDBOX_HOST:
        raise SpikeError("Refusing non-Sandbox PayPal endpoint")
    if parsed.username or parsed.password:
        raise SpikeError("Credentials must not be embedded in the endpoint URL")
    return url.rstrip("/")


class PayPal:
    def __init__(self, base_url: str):
        self.base_url = endpoint(base_url)
        self.client_id = os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise SpikeError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in the environment")
        self.access_token = self._access_token()

    def _access_token(self) -> str:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.base_url + "/v1/oauth2/token", data=b"grant_type=client_credentials", method="POST",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError):
            raise SpikeError("PayPal OAuth request failed; credentials and token were not printed") from None
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpikeError("PayPal OAuth response did not contain an access token")
        return token

    @staticmethod
    def _raise_http_error(exc: urllib.error.HTTPError) -> None:
        try:
            details = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, OSError):
            details = {}
        name = details.get("name", "")
        issue_codes = [item.get("issue", "") for item in details.get("details", [])]
        codes = ", ".join(code for code in [name, *issue_codes] if code)
        raise SpikeError(f"PayPal HTTP {exc.code}" + (f" ({codes})" if codes else "")) from None

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                request_id: str | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        if request_id:
            req.add_header("PayPal-Request-Id", request_id)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Deliberately do not print response bodies or request headers: they
            # may contain account details. The API error name/code is sufficient.
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError) as exc:
            # No automatic retry; a caller must reconcile before deciding what
            # to do because the operation may have reached PayPal.
            raise SpikeError(f"PayPal request outcome uncertain ({type(exc).__name__}); do not retry blindly") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise SpikeError("PayPal returned a non-JSON response") from None


def amount_text(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("amount must be a decimal USD amount") from None
    if amount not in (Decimal("1"), Decimal("10")):
        raise argparse.ArgumentTypeError("order amount must be exactly 1 or 10 USD")
    return f"{amount:.2f}"


def confirm(parser: argparse.ArgumentParser, flag: bool, message: str) -> None:
    if not flag:
        parser.error(f"{message}; pass --confirm to proceed")


def safe_order_summary(order: dict[str, Any]) -> dict[str, Any]:
    links = [link.get("href") for link in order.get("links", [])
             if link.get("rel") == "payer-action" or link.get("rel") == "approve"]
    return {"id": order.get("id"), "status": order.get("status"), "approval_urls": links}


def capture_summary(order: dict[str, Any]) -> dict[str, Any]:
    captures = (order.get("purchase_units") or [{}])[0].get("payments", {}).get("captures", [])
    if not captures:
        return {"order_id": order.get("id"), "status": order.get("status"), "captures": []}
    results = []
    for capture in captures:
        amount = capture.get("amount", {})
        breakdown = capture.get("seller_receivable_breakdown", {})
        gross = amount.get("value")
        fee = breakdown.get("paypal_fee", {})
        net = breakdown.get("net_amount", {})
        results.append({"capture_id": capture.get("id"), "status": capture.get("status"),
                        "gross": gross, "currency": amount.get("currency_code"),
                        "paypal_fee": fee.get("value"), "fee_currency": fee.get("currency_code"),
                        "net_amount": net.get("value"), "net_currency": net.get("currency_code"),
                        "seller_receivable_breakdown_fields": sorted(breakdown.keys())})
    return {"order_id": order.get("id"), "status": order.get("status"), "captures": results}


def parser_setup() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=SANDBOX, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("order-create", help="Create an approval order for exactly $1 or $10")
    create.add_argument("--amount", required=True, type=amount_text)
    create.add_argument("--confirm", action="store_true", help="confirm Sandbox order creation")

    capture = commands.add_parser("order-capture", help="Capture an approved order")
    capture.add_argument("order_id")
    capture.add_argument("--confirm", action="store_true", help="confirm Sandbox capture")

    show = commands.add_parser("order-show", help="Read order details; safe and read-only")
    show.add_argument("order_id")

    second = commands.add_parser("capture-again", help="Repeat capture once to record already-captured behavior")
    second.add_argument("order_id")
    second.add_argument("--confirm", action="store_true", help="confirm the intentional second capture call")

    payout = commands.add_parser("payout", help="Send one explicitly confirmed two-item Sandbox payout")
    payout.add_argument("--recipient", required=True, action="append", metavar="EMAIL",
                        help="Sandbox recipient email; provide exactly twice")
    payout.add_argument("--amount", required=True, action="append", metavar="USD",
                        help="positive USD amount; provide exactly twice")
    payout.add_argument("--sender-batch-id", help="unique experiment id; generated if omitted")
    payout.add_argument("--sender-item-id", action="append", metavar="ID",
                        help="item ID; provide twice (max 63 chars); otherwise generated")
    payout.add_argument("--confirm", action="store_true", help="confirm Sandbox payout")

    dup = commands.add_parser("duplicate-batch", help="Submit an explicitly repeated batch identifier")
    dup.add_argument("--recipient", required=True, action="append", metavar="EMAIL")
    dup.add_argument("--amount", required=True, action="append", metavar="USD")
    dup.add_argument("--sender-batch-id", required=True)
    dup.add_argument("--sender-item-id", action="append", metavar="ID")
    dup.add_argument("--confirm", action="store_true", help="confirm possible second Sandbox disbursement")

    detail = commands.add_parser("payout-show", help="Read payout batch fees and raw item status values")
    detail.add_argument("batch_id")

    return parser


def payout_items(recipients: list[str], amounts: list[str], ids: list[str] | None) -> list[dict[str, Any]]:
    if len(recipients) != 2 or len(amounts) != 2:
        raise SpikeError("payout requires exactly two --recipient and two --amount values")
    if ids is not None and len(ids) != 2:
        raise SpikeError("provide --sender-item-id exactly twice")
    result = []
    for index, (recipient, amount) in enumerate(zip(recipients, amounts)):
        try:
            parsed_amount = Decimal(amount)
        except InvalidOperation:
            raise SpikeError("each payout amount must be decimal USD") from None
        if not parsed_amount.is_finite():
            raise SpikeError("each payout amount must be finite")
        if parsed_amount <= 0 or parsed_amount.as_tuple().exponent < -2:
            raise SpikeError("payout amounts must be positive and have at most two decimal places")
        item_id = ids[index] if ids else f"t1-{uuid.uuid4().hex[:24]}-{index}"
        if not re.fullmatch(r"[^\s]{1,63}", item_id):
            raise SpikeError("sender_item_id must be 1-63 non-whitespace characters")
        result.append({"recipient_type": "EMAIL", "receiver": recipient,
                       "amount": {"value": f"{parsed_amount:.2f}", "currency": "USD"},
                       "sender_item_id": item_id, "note": "Dividend T1 Sandbox spike"})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = parser_setup()
    args = parser.parse_args(argv)
    try:
        base = endpoint(args.endpoint)
        if args.command == "order-create":
            confirm(parser, args.confirm, "order creation is a financial operation")
        elif args.command in {"order-capture", "capture-again"}:
            confirm(parser, args.confirm, "capture is a financial operation")
        elif args.command in {"payout", "duplicate-batch"}:
            confirm(parser, args.confirm, "payout may move money")
        if args.command in {"payout", "duplicate-batch"}:
            if args.command == "duplicate-batch" and args.sender_item_id is None:
                raise SpikeError("duplicate-batch requires two explicit --sender-item-id values reused on the second call")
            items = payout_items(args.recipient, args.amount, args.sender_item_id)
            batch_id = getattr(args, "sender_batch_id", None) or f"dividend-t1-{uuid.uuid4()}"
            if not batch_id or len(batch_id) > 256:
                raise SpikeError("sender_batch_id must be 1-256 characters")
            print(json.dumps({"sender_batch_id": batch_id,
                              "sender_item_ids": [item["sender_item_id"] for item in items],
                              "amounts_usd": args.amount, "recipients": args.recipient}, indent=2), flush=True)
        client = PayPal(base)
        if args.command == "order-create":
            experiment = f"dividend-t1-{uuid.uuid4()}"
            print(json.dumps({"experiment_id": experiment, "amount": args.amount, "currency": "USD"}), flush=True)
            body = {"intent": "CAPTURE", "purchase_units": [{"reference_id": experiment,
                     "amount": {"currency_code": "USD", "value": args.amount}}],
                    "application_context": {"brand_name": "Dividend Sandbox Spike",
                     "user_action": "PAY_NOW"}}
            result = client.request("POST", "/v2/checkout/orders", body, experiment)
            print(json.dumps({"experiment_id": experiment, **safe_order_summary(result)}, indent=2))
        elif args.command == "order-capture":
            result = client.request("POST", f"/v2/checkout/orders/{urllib.parse.quote(args.order_id, safe='')}/capture", {},
                                    f"dividend-t1-capture-{uuid.uuid4()}")
            print(json.dumps(capture_summary(result), indent=2))
        elif args.command == "order-show":
            result = client.request("GET", f"/v2/checkout/orders/{urllib.parse.quote(args.order_id, safe='')}")
            print(json.dumps(capture_summary(result), indent=2))
        elif args.command == "payout-show":
            result = client.request("GET", f"/v1/payments/payouts/{urllib.parse.quote(args.batch_id, safe='')}")
            header = result.get("batch_header", {})
            items = result.get("items", [])
            print(json.dumps({"batch_id": header.get("payout_batch_id"),
                              "batch_status": header.get("batch_status"),
                              "fees": header.get("fees"),
                              "raw_batch_fields": sorted(header.keys()),
                              "items": [{"sender_item_id": (item.get("payout_item") or {}).get("sender_item_id"),
                                         "status": item.get("transaction_status"),
                                         "payout_item_id": item.get("payout_item_id"),
                                         "errors": (item.get("errors") or {}).get("name")}
                                        for item in items]}, indent=2))
        elif args.command == "capture-again":
            result = client.request("POST", f"/v2/checkout/orders/{urllib.parse.quote(args.order_id, safe='')}/capture", {},
                                    f"dividend-t1-second-capture-{uuid.uuid4()}")
            print(json.dumps(capture_summary(result), indent=2))
        else:
            result = client.request("POST", "/v1/payments/payouts", {
                "sender_batch_header": {"sender_batch_id": batch_id,
                 "email_subject": "Dividend Sandbox spike", "email_message": "Sandbox payout experiment"},
                "items": items,
            }, f"dividend-t1-payout-{uuid.uuid4()}")
            batch = result.get("batch_header", {})
            print(json.dumps({"sender_batch_id": batch_id, "batch_id": batch.get("payout_batch_id"),
                              "batch_status": batch.get("batch_status"),
                              "item_statuses": [{"sender_item_id": item.get("payout_item", {}).get("sender_item_id"),
                                                 "transaction_status": item.get("transaction_status"),
                                                 "payout_item_id": item.get("payout_item_id"),
                                                 "errors": (item.get("errors") or {}).get("name")}
                                                for item in result.get("items", [])],
                              "raw_batch_fields": sorted(batch.keys()),
                              "note": "PayPal fee values may require GET /v1/payments/payouts/{batch_id}; use payout-show."}, indent=2))
    except SpikeError as exc:
        print(f"spike: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
