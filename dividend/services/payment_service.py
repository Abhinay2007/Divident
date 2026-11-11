"""Payment orchestration; only this service calls PaymentsProvider."""

from datetime import UTC, datetime
import re

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DbSession

from ..allocation.policy import load_allocation_policy
from ..errors import (InvariantViolation, NotFoundError, PaymentProviderError,
                      StateError, ValidationError)
from ..ledger import append as append_ledger_event
from ..models import (LedgerEvent, Maintainer, MaintainerRecipient,
                      PaymentOperation, PayoutBatch, PayoutItem, Plan, PlanLine)
from ..schemas import (ApproveResponse, BatchResponse, CaptureDetail,
                       CaptureResponse, ItemResponse, PayoutResponse,
                       PayoutsResponse)
from ..payments.base import (BatchDetail, BatchResult, CaptureResult, OrderDetail,
                             OrderResult, PaymentsProvider, PayoutItemInput)
from ..payments.reconcile import map_batch_status, map_item_status
from .plan_service import PlanService

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Explicit compatibility scope for the three pre-snapshot Sandbox payout records.
_LEGACY_PAYOUTS = {
    "0619cc2d-8b5c-4a90-b709-0ebe07acb7f8": (
        "SWFXHXW893NQY", "p0619cc2d-a1-000", "BQ3NESV9D4BS6", 750, "settled"),
    "2f313a2f-f738-476f-9af2-9e2f970b8918": (
        "F27CU8WP649RA", "p2f313a2f-a1-000", "FMAPC8HZCXVCY", 1200, "settled"),
    "cf86a05d-f5a6-43ea-ad7f-ca6936dc0219": (
        "VD4E4K8Q7SWM4", "pcf86a05d-a1-000", "K9JXFBAHPXXCS", 750, "paying"),
}


def _new_session(db: DbSession) -> DbSession:
    return DbSession(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def _provider_failure(exc: Exception) -> PaymentProviderError:
    if isinstance(exc, PaymentProviderError):
        return exc
    return PaymentProviderError("Payment provider call failed", code="provider_error")


def _begin_immediate(tx: DbSession) -> None:
    connection = tx.connection()
    if connection.dialect.name != "sqlite" or connection.connection.driver_connection.in_transaction:
        return
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    except OperationalError as exc:
        if "locked" in str(exc.orig).lower():
            raise StateError("Payment state is busy; retry the request", code="payment_operation_busy",
                             current_state="unknown", allowed_transitions=[]) from exc
        raise


def _available_funds_cents(tx: DbSession) -> int:
    plans = list(tx.scalars(select(Plan).where(Plan.capture_net_cents.is_not(None))))
    captured = sum(plan.capture_net_cents or 0 for plan in plans)
    failed_plan_reserve = sum(
        plan.capture_net_cents or 0 for plan in plans if plan.state == "failed"
    )
    batches = list(tx.scalars(select(PayoutBatch)))
    items_by_batch = {
        batch.id: list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)))
        for batch in batches
    }
    spent = sum(item.amount_cents for rows in items_by_batch.values()
                for item in rows if item.status == "success")
    reserved = sum(item.amount_cents for rows in items_by_batch.values()
                   for item in rows if item.status in {"pending", "unclaimed", "needs_attention"})
    fees = 0
    for batch in batches:
        if batch.fees_cents is not None:
            fees += batch.fees_cents
        elif batch.estimated_fees_cents is not None:
            rows = items_by_batch[batch.id]
            if rows:
                item_fee = batch.estimated_fees_cents // len(rows)
                active = sum(item.status not in {"failed", "returned"} for item in rows)
                fees += item_fee * active
        elif batch.status not in {"intent", "rejected"}:
            raise StateError("Payout fee is unresolved; available funds cannot be established",
                             code="available_funds_unknown", current_state="unknown",
                             allowed_transitions=[])
    return captured - failed_plan_reserve - spent - reserved - fees


def _recipient_by_id(tx: DbSession, maintainer_id: str):
    recipient = tx.get(MaintainerRecipient, maintainer_id)
    if recipient is not None:
        return recipient
    return tx.get(Maintainer, maintainer_id)


def _eligible_recipient(recipient, upstream_key: str) -> bool:
    return bool(
        recipient is not None
        and recipient.upstream_key == upstream_key
        and recipient.opted_in is True
        and recipient.active is True
        and recipient.market_code == "US"
        and isinstance(recipient.email, str)
        and _EMAIL.fullmatch(recipient.email)
    )


class PaymentService:
    def __init__(self, provider: PaymentsProvider, public_base_url: str,
                 allocation_policy_path=None):
        self.provider = provider
        self.public_base_url = str(public_base_url).rstrip("/")
        self.policy = load_allocation_policy(allocation_policy_path) if allocation_policy_path is not None else load_allocation_policy()

    def approve_and_fund(self, plan_id: str, db: DbSession) -> ApproveResponse:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            PlanService.verify_approval_proposal(tx, plan)
            if plan.state == "cancelled":
                raise StateError("Cancelled plan has no active approval link", code="plan_cancelled",
                                 current_state=plan.state, allowed_transitions=[])
            self._require_measured_policy()
            if plan.order_id and plan.approval_url:
                return ApproveResponse(order_id=plan.order_id, approval_url=plan.approval_url)
            if plan.state == "funding":
                raise PaymentProviderError("Order outcome must be reconciled before another create request",
                                           code="order_outcome_unknown", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            if plan.charge_cents is None or plan.est_payout_fees_cents is None:
                raise ValidationError("Funding charge is unavailable until fee inputs are measured",
                                      code="charge_unavailable")
            if type(plan.charge_cents) is not int or plan.charge_cents <= 0:
                raise InvariantViolation("Plan charge must be positive integer cents", code="invalid_plan_charge")
            if plan.state == "draft":
                PlanService.approve(tx, plan_id)
            else:
                raise StateError("Plan cannot be funded from its current state", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["funding"])
            tx.commit()
            attempt, amount = plan.order_attempt, plan.charge_cents
            existing_order_id = plan.order_id
            existing_url = plan.approval_url
        if existing_order_id and existing_url:
            return ApproveResponse(order_id=existing_order_id, approval_url=existing_url)

        request_id = f"plan:{plan_id}:order:{attempt}"
        try:
            order = self.provider.create_order(
                plan_id, amount, "USD", request_id,
                f"{self.public_base_url}/fund/{plan_id}?paypal=return",
                f"{self.public_base_url}/fund/{plan_id}?paypal=cancel",
            )
        except Exception as exc:
            raise _provider_failure(exc) from exc
        if not isinstance(order, OrderResult) or not order.order_id or not order.approval_url:
            raise PaymentProviderError("Provider returned an invalid order", code="invalid_order_response")

        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            if plan.order_id is None:
                plan.order_id, plan.approval_url = order.order_id, order.approval_url
                append_ledger_event(tx, "ORDER_CREATED", {"order_id": order.order_id,
                                   "amount_cents": amount, "currency": "USD"},
                                   plan_id=plan_id, session_id=plan.session_id, external_id=order.order_id)
                tx.commit()
            return ApproveResponse(order_id=plan.order_id, approval_url=plan.approval_url)

    def capture(self, plan_id: str, db: DbSession) -> CaptureResponse:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            PlanService.verify_approval_proposal(tx, plan)
            if plan.state == "funded" and plan.capture_gross_cents is not None:
                event = tx.scalar(select(LedgerEvent).where(
                    LedgerEvent.plan_id == plan_id, LedgerEvent.type == "ORDER_CAPTURED"
                ).order_by(LedgerEvent.created_at.desc()).limit(1))
                capture_id = event.payload.get("capture_id", "") if event else ""
                if (not capture_id or type(plan.capture_gross_cents) is not int
                        or type(plan.capture_net_cents) is not int):
                    raise PaymentProviderError("Stored capture facts are incomplete",
                                               code="capture_recovery_failed", retry_hint="reconcile")
                result = CaptureResult(capture_id, plan.capture_gross_cents, plan.capture_fee_cents,
                                       plan.capture_net_cents, "USD", "COMPLETED")
                self._validate_capture(result)
                return self._capture_response(result, plan.state)
            if plan.state != "funding" or not plan.order_id:
                raise StateError("Plan has no capturable order", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["funding"])
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id,
                PaymentOperation.kind == "capture",
            ).order_by(PaymentOperation.attempt.desc()).limit(1))
            order_id = plan.order_id

        if operation is not None:
            if operation.state in {"unknown", "submitting"}:
                return self._recover_capture(plan_id, order_id, operation.id, db)
            if operation.state in {"confirmed", "resolved"}:
                capture = self._capture_from_operation(operation)
                if capture is None:
                    return self._recover_capture(plan_id, order_id, operation.id, db)
                if self.policy.fees.measured:
                    return self._apply_stored_capture(plan_id, operation.id, capture, db)
                return self._capture_response(capture, "funding")
            if operation.state == "intent":
                self._require_measured_policy()
                attempt = operation.attempt
            else:
                raise PaymentProviderError("Capture operation cannot be submitted again",
                                           code="capture_operation_unavailable",
                                           retry_hint="reconcile")
        else:
            self._require_measured_policy()
        payer_country_code = None
        if operation is None or operation.state == "intent":
            try:
                order_detail = self.provider.get_order(order_id)
            except Exception as exc:
                raise _provider_failure(exc) from exc
            if not isinstance(order_detail, OrderDetail) or order_detail.order_id != order_id:
                raise PaymentProviderError("Provider returned an invalid order before capture",
                                           code="invalid_order_response")
            payer_country_code = order_detail.payer_country_code
            if payer_country_code != "US":
                raise ValidationError("Only verified US payer transactions are supported",
                                      code="unsupported_transaction_scope")
            if order_detail.captures:
                if operation is None:
                    raise PaymentProviderError("Order already has a capture without a local operation",
                                               code="capture_operation_missing",
                                               retry_hint="reconcile",
                                               detail={"outcome_unknown": True})
                return self._recover_capture(plan_id, order_id, operation.id, db)
        if operation is None:
            attempt = self._claim_capture(plan_id, order_id, db)

        self._mark_capture_submitting(plan_id, attempt, order_id, db)
        request_id = f"plan:{plan_id}:capture:{attempt}"
        try:
            capture = self.provider.capture_order(order_id, request_id)
        except Exception as exc:
            failure = _provider_failure(exc)
            self._record_capture_failure(plan_id, attempt, failure, db)
            raise failure from exc
        self._validate_capture(capture)
        return self._persist_capture(plan_id, attempt, capture, db, recovered=False,
                                     expected_order_id=order_id,
                                     payer_country_code=payer_country_code)

    def _claim_capture(self, plan_id: str, order_id: str, db: DbSession) -> int:
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            if plan.state != "funding" or plan.order_id != order_id:
                raise StateError("Plan is no longer awaiting capture", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["funding"])
            if plan.charge_cents is None or plan.est_payout_fees_cents is None:
                raise ValidationError("Capture requirement is unavailable", code="charge_unavailable")
            existing = tx.scalar(select(PaymentOperation.id).where(
                PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "capture"))
            if existing is not None or plan.capture_attempt != 0:
                raise PaymentProviderError("A capture attempt already exists and must be recovered",
                                           code="capture_outcome_unknown", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            attempt = plan.capture_attempt + 1
            request_id = f"plan:{plan_id}:capture:{attempt}"
            plan.capture_attempt = attempt
            tx.add(PaymentOperation(
                plan_id=plan_id, kind="capture", attempt=attempt, state="intent",
                request_id=request_id,
            ))
            tx.commit()
            return attempt

    def _mark_capture_submitting(self, plan_id: str, attempt: int, order_id: str,
                                 db: DbSession) -> None:
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id,
                PaymentOperation.kind == "capture",
                PaymentOperation.attempt == attempt,
            ))
            if (plan.state != "funding" or plan.order_id != order_id or operation is None
                    or operation.state != "intent"):
                raise StateError("Capture authorization is no longer available",
                                 code="capture_operation_unavailable", current_state=plan.state,
                                 allowed_transitions=["funding"])
            operation.state = "submitting"
            operation.updated_at = datetime.now(UTC)
            tx.commit()

    def _record_capture_failure(self, plan_id: str, attempt: int,
                                failure: PaymentProviderError, db: DbSession) -> None:
        unknown = failure.detail.get("outcome_unknown", True)
        state = "unknown" if unknown else "rejected"
        with _new_session(db) as tx:
            _begin_immediate(tx)
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id,
                PaymentOperation.kind == "capture",
                PaymentOperation.attempt == attempt,
            ))
            if operation is not None and operation.state == "submitting":
                operation.state = state
                operation.error_code = failure.code[:64]
                operation.updated_at = datetime.now(UTC)
                append_ledger_event(tx, "CAPTURE_OUTCOME_UNKNOWN" if unknown else "CAPTURE_REJECTED", {
                    "attempt": attempt, "code": failure.code,
                }, plan_id=plan_id, external_id=operation.request_id)
                tx.commit()

    def _recover_capture(self, plan_id: str, order_id: str, operation_id: str,
                         db: DbSession) -> CaptureResponse:
        try:
            order = self.provider.get_order(order_id)
        except Exception as exc:
            raise _provider_failure(exc) from exc
        if (not isinstance(order, OrderDetail) or order.order_id != order_id
                or not isinstance(order.captures, list)):
            raise PaymentProviderError("Provider returned an invalid order during capture recovery",
                                       code="invalid_order_response", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        if not order.captures:
            raise PaymentProviderError("Capture outcome remains unknown", code="capture_outcome_unknown",
                                       retry_hint="reconcile", detail={"outcome_unknown": True})
        if len(order.captures) != 1:
            raise PaymentProviderError("Order recovery returned an unexpected number of captures",
                                       code="invalid_capture_response", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        capture = order.captures[0]
        self._validate_capture(capture)
        if capture.status.upper() != "COMPLETED":
            raise PaymentProviderError("Capture is not yet completed", code="capture_outcome_unknown",
                                       retry_hint="reconcile", detail={"outcome_unknown": True})
        return self._persist_capture(plan_id, None, capture, db, recovered=True,
                                     operation_id=operation_id, expected_order_id=order_id,
                                     payer_country_code=order.payer_country_code)

    def _persist_capture(self, plan_id: str, attempt: int | None, capture: CaptureResult,
                         db: DbSession, *, recovered: bool, operation_id: str | None = None,
                         expected_order_id: str | None = None,
                         payer_country_code: str | None = None) -> CaptureResponse:
        payload = self._capture_payload(capture)
        payload["payer_country_code"] = payer_country_code
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            operation = (tx.get(PaymentOperation, operation_id) if operation_id else tx.scalar(
                select(PaymentOperation).where(
                    PaymentOperation.plan_id == plan_id,
                    PaymentOperation.kind == "capture",
                    PaymentOperation.attempt == attempt,
                )))
            if operation is None or (expected_order_id is not None and plan.order_id != expected_order_id):
                raise PaymentProviderError("Capture recovery no longer matches the plan",
                                           code="capture_recovery_conflict", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            if operation.external_id and operation.external_id != capture.capture_id:
                raise PaymentProviderError("Recovered capture identity conflicts with stored operation",
                                           code="capture_recovery_conflict", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            if plan.state != "funding":
                if plan.state in {"funded", "failed"} and operation.external_id == capture.capture_id:
                    return self._capture_response(capture, plan.state)
                raise PaymentProviderError("Capture recovery no longer matches the plan",
                                           code="capture_recovery_conflict", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            allowed_operation_states = {"submitting"} if not recovered else {
                "intent", "submitting", "unknown", "confirmed", "resolved",
            }
            if operation.state not in allowed_operation_states:
                raise PaymentProviderError("Capture operation state changed during recovery",
                                           code="capture_recovery_conflict", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            operation.external_id = capture.capture_id
            operation.provider_result = payload
            operation.state = "resolved" if recovered else "confirmed"
            operation.updated_at = datetime.now(UTC)
            operation.resolved_at = datetime.now(UTC)
            if recovered:
                append_ledger_event(tx, "CAPTURE_RECOVERED", payload, plan_id=plan_id,
                                    external_id=capture.capture_id)
            if self.policy.fees.measured:
                plan = PlanService.mark_captured(tx, plan_id, capture,
                                                 payer_market_code=payer_country_code)
                state = plan.state
                tx.commit()
            else:
                tx.commit()
                state = plan.state
        return self._capture_response(capture, state)

    def _apply_stored_capture(self, plan_id: str, operation_id: str,
                              capture: CaptureResult, db: DbSession) -> CaptureResponse:
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            operation = tx.get(PaymentOperation, operation_id)
            if operation is None or operation.external_id != capture.capture_id:
                raise PaymentProviderError("Stored capture identity is inconsistent",
                                           code="capture_recovery_conflict")
            if plan.state != "funding":
                raise StateError("Plan is not awaiting capture", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["funded"])
            market = operation.provider_result.get("payer_country_code") if isinstance(
                operation.provider_result, dict) else None
            plan = PlanService.mark_captured(tx, plan_id, capture,
                                             payer_market_code=market)
            state = plan.state
            tx.commit()
        return self._capture_response(capture, state)

    @staticmethod
    def _capture_payload(capture: CaptureResult) -> dict:
        return {
            "capture_id": capture.capture_id,
            "gross_cents": capture.gross_cents,
            "fee_cents": capture.fee_cents,
            "net_cents": capture.net_cents,
            "currency": capture.currency,
            "status": capture.status,
        }

    @classmethod
    def _validate_capture(cls, capture: CaptureResult) -> None:
        payload = cls._capture_payload(capture) if isinstance(capture, CaptureResult) else None
        if (payload is None or type(payload["capture_id"]) is not str or not payload["capture_id"]
                or any(type(payload[key]) is not int or payload[key] < 0
                       for key in ("gross_cents", "net_cents"))
                or (payload["fee_cents"] is not None and
                    (type(payload["fee_cents"]) is not int or payload["fee_cents"] < 0))
                or type(payload["currency"]) is not str or type(payload["status"]) is not str):
            raise PaymentProviderError("Provider returned an invalid capture", code="invalid_capture_response",
                                       retry_hint="reconcile", detail={"outcome_unknown": True})

    @staticmethod
    def _capture_from_operation(operation: PaymentOperation) -> CaptureResult | None:
        data = operation.provider_result
        if not isinstance(data, dict):
            return None
        try:
            result = CaptureResult(
                capture_id=data["capture_id"], gross_cents=data["gross_cents"],
                fee_cents=data.get("fee_cents"), net_cents=data["net_cents"],
                currency=data["currency"], status=data["status"],
            )
        except (KeyError, TypeError):
            return None
        try:
            PaymentService._validate_capture(result)
        except PaymentProviderError:
            return None
        if operation.external_id != result.capture_id:
            return None
        return result

    @staticmethod
    def _capture_response(capture: CaptureResult, state: str) -> CaptureResponse:
        return CaptureResponse(capture=CaptureDetail(
            capture_id=capture.capture_id, gross_cents=capture.gross_cents,
            fee_cents=capture.fee_cents, net_cents=capture.net_cents,
            currency=capture.currency, status=capture.status,
        ), state=state)

    def payout(self, plan_id: str, db: DbSession) -> PayoutResponse:
        return self._dispatch_payout(plan_id, db, retry=False)

    def retry_payout(self, plan_id: str, db: DbSession) -> PayoutResponse:
        return self._dispatch_payout(plan_id, db, retry=True)

    def _dispatch_payout(self, plan_id: str, db: DbSession, *, retry: bool) -> PayoutResponse:
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            PlanService.verify_approval_proposal(tx, plan)
            self._require_measured_policy()
            snapshot = plan.fee_policy_snapshot
            assumptions = snapshot.get("assumptions", {}) if isinstance(snapshot, dict) else {}
            if (not isinstance(snapshot, dict) or snapshot.get("supported") is not True
                    or snapshot.get("version") != plan.fee_policy_version
                    or type(assumptions.get("payout_fee_per_item_cents")) is not int):
                raise ValidationError("Plan has no supported frozen fee policy", code="fee_policy_unavailable")
            latest = tx.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                               .order_by(PayoutBatch.attempt.desc()).limit(1))
            if (not retry and latest is not None and latest.external_id is not None
                    and plan.state in {"paying", "settled", "settled_with_exceptions"}):
                return PayoutResponse(batch_id=latest.external_id,
                                      item_count=len(latest.items), state=latest.status)
            if plan.state == "paying" and latest is not None:
                batch = latest
                if batch.external_id:
                    return PayoutResponse(batch_id=batch.external_id,
                                          item_count=len(batch.items), state=batch.status)
                operation = tx.scalar(select(PaymentOperation).where(
                    PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout",
                    PaymentOperation.attempt == batch.attempt,
                ))
                if operation is None or operation.payout_batch_id != batch.id:
                    raise PaymentProviderError("Payout intent has no matching operation",
                                               code="payout_operation_missing", retry_hint="reconcile")
                if operation.state != "intent":
                    if operation.state in {"submitting", "unknown"} and batch.status != "needs_attention":
                        batch.status = "needs_attention"
                        tx.commit()
                    return PayoutResponse(batch_id=batch.sender_batch_id,
                                          item_count=len(batch.items), state=batch.status)
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)
                                        .order_by(PayoutItem.sender_item_id)))
                sender_batch_id, attempt = batch.sender_batch_id, batch.attempt
            else:
                expected_state = "settled_with_exceptions" if retry else "funded"
                if plan.state != expected_state:
                    raise StateError("Plan cannot dispatch payouts from its current state",
                                     code="invalid_plan_transition", current_state=plan.state,
                                     allowed_transitions=[expected_state])
                lines = list(tx.scalars(select(PlanLine).where(
                    PlanLine.plan_id == plan_id, PlanLine.payable_cents > 0
                ).order_by(PlanLine.upstream_key)))
                line_by_key = {line.upstream_key: line for line in lines}
                if retry:
                    if latest is None or not latest.external_id:
                        raise StateError("A confirmed prior payout batch is required for retry",
                                         code="payout_batch_unknown", current_state=plan.state,
                                         allowed_transitions=[])
                    all_items = list(tx.scalars(select(PayoutItem).where(
                        PayoutItem.plan_id == plan_id)))
                    if any(item.status in {"pending", "needs_attention"} for item in all_items):
                        raise StateError("A pending or unknown payout must be reconciled before retry",
                                         code="payout_outcome_unresolved", current_state=plan.state,
                                         allowed_transitions=[])
                    failed_items = list(tx.scalars(select(PayoutItem).where(
                        PayoutItem.batch_id == latest.id, PayoutItem.status == "failed"
                    ).order_by(PayoutItem.sender_item_id)))
                    recipient_specs = []
                    for item in failed_items:
                        line = line_by_key.get(item.upstream_key)
                        if line is None:
                            raise InvariantViolation("Failed payout item has no matching plan line",
                                                     code="payout_item_mismatch")
                        splits = line.maintainer_splits or []
                        split = next((row for row in splits
                                      if isinstance(row, dict)
                                      and row.get("maintainer_id") == item.maintainer_id), None)
                        if item.maintainer_id is None:
                            legacy = tx.scalar(select(Maintainer).where(
                                Maintainer.upstream_key == item.upstream_key,
                            ))
                            if legacy is not None and (not splits or any(
                                row.get("maintainer_id") == legacy.id for row in splits
                            )):
                                split = {"maintainer_id": legacy.id,
                                         "amount_cents": line.payable_cents}
                        elif split is None:
                            legacy = tx.get(Maintainer, item.maintainer_id)
                            if (legacy is not None and legacy.upstream_key == item.upstream_key
                                    and item.amount_cents == line.payable_cents):
                                split = {"maintainer_id": legacy.id,
                                         "amount_cents": line.payable_cents}
                        if (split is None or split.get("amount_cents") != item.amount_cents
                                or item.maintainer_id not in (None, split.get("maintainer_id"))):
                            raise InvariantViolation("Failed payout amount no longer matches the frozen recipient split",
                                                     code="payout_item_mismatch")
                        recipient = _recipient_by_id(tx, split["maintainer_id"])
                        if not _eligible_recipient(recipient, item.upstream_key):
                            raise InvariantViolation("Failed payout recipient is no longer eligible",
                                                     code="invalid_payout_recipient")
                        recipient_specs.append({
                            "upstream_key": item.upstream_key,
                            "maintainer_id": split["maintainer_id"],
                            "display_name": recipient.display_name,
                            "email": recipient.email,
                            "amount_cents": item.amount_cents,
                            "share_bps": split.get("share_bps", 10000),
                        })
                else:
                    recipient_specs = []
                    for line in lines:
                        splits = line.maintainer_splits
                        if splits is None:
                            # Pre-upgrade/manual legacy lines used one project recipient.
                            legacy = tx.scalar(select(Maintainer).where(
                                Maintainer.upstream_key == line.upstream_key,
                            ))
                            splits = ([{"maintainer_id": legacy.id,
                                       "display_name": legacy.display_name,
                                       "share_bps": 10000,
                                       "amount_cents": line.payable_cents}]
                                      if legacy is not None and _eligible_recipient(legacy, line.upstream_key)
                                      else [])
                        if (not isinstance(splits, list) or not splits
                                or any(not isinstance(row, dict) for row in splits)
                                or sum(row.get("amount_cents", 0) for row in splits) != line.payable_cents
                                or sum(row.get("share_bps", 0) for row in splits) != 10000):
                            raise InvariantViolation("Payable allocation has no valid frozen recipient split",
                                                     code="invalid_payout_recipient")
                        seen_ids = set()
                        for split in splits:
                            if (not isinstance(split, dict)
                                    or type(split.get("amount_cents")) is not int
                                    or split["amount_cents"] <= 0
                                    or type(split.get("share_bps")) is not int
                                    or split["maintainer_id"] in seen_ids):
                                raise InvariantViolation("Frozen recipient split is invalid",
                                                         code="invalid_payout_recipient")
                            seen_ids.add(split["maintainer_id"])
                            recipient = _recipient_by_id(tx, split["maintainer_id"])
                            if not _eligible_recipient(recipient, line.upstream_key):
                                raise InvariantViolation("Payable allocation has no eligible recipient",
                                                         code="invalid_payout_recipient")
                            recipient_specs.append({
                                "upstream_key": line.upstream_key,
                                "maintainer_id": split["maintainer_id"],
                                "display_name": recipient.display_name,
                                "email": recipient.email,
                                "amount_cents": split["amount_cents"],
                                "share_bps": split["share_bps"],
                            })
                if not recipient_specs:
                    raise StateError("Plan has no eligible failed payout allocations" if retry else "Plan has no payable allocations",
                                     code="no_failed_payout_items" if retry else "no_payable_allocations",
                                     current_state=plan.state, allowed_transitions=[])
                available = _available_funds_cents(tx)
                payout_fee_estimate = assumptions["payout_fee_per_item_cents"] * len(recipient_specs)
                required = sum(item["amount_cents"] for item in recipient_specs) + payout_fee_estimate
                if available < required:
                    raise StateError("Tracked available funds do not cover payout items and estimated fees",
                                     code="insufficient_available_funds", current_state=plan.state,
                                     allowed_transitions=[])
                attempt = plan.payout_attempt + 1
                sender_batch_id = f"plan_{plan_id}_payout_{attempt}"
                batch = PayoutBatch(plan_id=plan_id, attempt=attempt,
                                    sender_batch_id=sender_batch_id, status="intent",
                                    estimated_fees_cents=payout_fee_estimate)
                tx.add(batch)
                tx.flush()
                items = []
                for index, spec in enumerate(recipient_specs):
                    item = PayoutItem(
                        batch_id=batch.id, plan_id=plan_id, upstream_key=spec["upstream_key"],
                        maintainer_id=spec["maintainer_id"],
                        sender_item_id=f"p{plan_id[:8]}-a{attempt}-{index:03d}",
                        recipient_email=spec["email"],
                        amount_cents=spec["amount_cents"], status="pending",
                    )
                    tx.add(item)
                    items.append(item)
                    append_ledger_event(tx, "PAYOUT_ITEM_INTENT", {
                        "sender_item_id": item.sender_item_id,
                        "maintainer_id": spec["maintainer_id"],
                        "upstream_key": spec["upstream_key"],
                        "share_bps": spec["share_bps"],
                        "amount_cents": spec["amount_cents"],
                    }, plan_id=plan_id, external_id=item.sender_item_id)
                plan.payout_attempt = attempt
                operation = PaymentOperation(
                    plan_id=plan_id, kind="payout", attempt=attempt, state="intent",
                    request_id=f"plan:{plan_id}:payout:{attempt}", payout_batch_id=batch.id,
                    prior_plan_state=expected_state,
                )
                tx.add(operation)
                append_ledger_event(tx, "PAYOUT_FEE_ESTIMATED", {
                    "estimated_cents": payout_fee_estimate, "item_count": len(items),
                    "policy_version": plan.fee_policy_version,
                }, plan_id=plan_id, external_id=sender_batch_id)
                PlanService.transition(tx, plan_id, expected_state, "paying", "PAYOUT_INTENT", {
                    "attempt": attempt, "sender_batch_id": sender_batch_id,
                    "item_count": len(items), "recipient_count": len(items),
                }, additional_events=[])
                # transition() committed the intent and all items atomically.
                batch = tx.get(PayoutBatch, batch.id)
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)
                                        .order_by(PayoutItem.sender_item_id)))
            sender_batch_id, attempt = batch.sender_batch_id, batch.attempt
            inputs = [PayoutItemInput(item.sender_item_id, item.recipient_email,
                                      item.amount_cents, "USD") for item in items]

        with _new_session(db) as tx:
            _begin_immediate(tx)
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout",
                PaymentOperation.attempt == attempt,
            ))
            batch = tx.scalar(select(PayoutBatch).where(
                PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt,
            ))
            plan = PlanService.get(tx, plan_id)
            if (operation is None or batch is None or plan.payout_attempt != attempt
                    or operation.payout_batch_id != batch.id or operation.state != "intent"
                    or batch.external_id is not None):
                raise StateError("Payout submission authorization is no longer available",
                                 code="payout_operation_unavailable", current_state=plan.state,
                                 allowed_transitions=[])
            operation.state = "submitting"
            operation.updated_at = datetime.now(UTC)
            batch.status = "submitting"
            tx.commit()

        try:
            result = self.provider.create_payout_batch(
                sender_batch_id, f"plan:{plan_id}:payout:{attempt}", inputs,
            )
        except Exception as exc:
            failure = _provider_failure(exc)
            with _new_session(db) as tx:
                _begin_immediate(tx)
                batch = tx.scalar(select(PayoutBatch).where(
                    PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
                operation = tx.scalar(select(PaymentOperation).where(
                    PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout",
                    PaymentOperation.attempt == attempt,
                ))
                if batch is not None and operation is not None and batch.external_id is None:
                    unknown = failure.detail.get("outcome_unknown", True)
                    if unknown:
                        batch.status = "needs_attention"
                        operation.state = "unknown"
                        append_ledger_event(tx, "PAYOUT_OUTCOME_UNKNOWN", {
                            "sender_batch_id": sender_batch_id, "code": failure.code,
                        }, plan_id=plan_id, external_id=sender_batch_id)
                    elif failure.detail.get("status_code") is not None:
                        batch.status = "failed"
                        operation.state = "rejected"
                        operation.error_code = failure.code[:64]
                        operation.resolved_at = datetime.now(UTC)
                        append_ledger_event(tx, "PAYOUT_REJECTED", {
                            "sender_batch_id": sender_batch_id, "code": failure.code,
                        }, plan_id=plan_id, external_id=sender_batch_id)
                    else:
                        batch.status = "intent"
                        operation.state = "intent"
                    operation.updated_at = datetime.now(UTC)
                    tx.commit()
            raise failure from exc
        if not isinstance(result, BatchResult) or not result.batch_id:
            raise PaymentProviderError("Provider returned an invalid payout batch", code="invalid_payout_response")

        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            batch = tx.scalar(select(PayoutBatch).where(
                PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout",
                PaymentOperation.attempt == attempt,
            ))
            if (batch is None or operation is None or plan.payout_attempt != attempt
                    or operation.payout_batch_id != batch.id or operation.state != "submitting"):
                raise StateError("Payout result no longer matches its operation",
                                 code="payout_operation_conflict", current_state=plan.state,
                                 allowed_transitions=[])
            if ((batch.external_id is not None and batch.external_id != result.batch_id)
                    or (operation.external_id is not None and operation.external_id != result.batch_id)):
                raise PaymentProviderError("Provider batch ID conflicts with its payout operation",
                                           code="payout_batch_mismatch", retry_hint="reconcile",
                                           detail={"outcome_unknown": True})
            if batch.external_id is None:
                batch.external_id = result.batch_id
                batch.status = map_batch_status(result.status)
                batch.fees_cents = result.fees_cents
                operation.external_id = result.batch_id
                operation.state = "confirmed"
                operation.provider_result = {"status": result.status, "fees_cents": result.fees_cents}
                operation.updated_at = datetime.now(UTC)
                operation.resolved_at = datetime.now(UTC)
                append_ledger_event(tx, "PAYOUT_BATCH_CREATED", {
                    "batch_id": result.batch_id, "status": batch.status,
                    "item_count": len(inputs),
                }, plan_id=plan_id, external_id=result.batch_id)
                if result.fees_cents is not None:
                    append_ledger_event(tx, "PAYOUT_FEE_ACTUALIZED", {
                        "estimated_cents": batch.estimated_fees_cents,
                        "actual_cents": result.fees_cents,
                    }, plan_id=plan_id, external_id=result.batch_id)
                tx.commit()
            saved_batch_id = batch.external_id
        if saved_batch_id != result.batch_id:
            raise PaymentProviderError("Provider batch ID conflicts with the saved payout operation",
                                       code="payout_batch_mismatch", retry_hint="reconcile",
                                       detail={"outcome_unknown": True})
        # A POST response confirms creation only; item outcomes come from GET.
        self.reconcile(plan_id, db)
        return PayoutResponse(batch_id=saved_batch_id, item_count=len(items),
                              state=self._batch_status(plan_id, attempt, db))

    def reconcile(self, plan_id: str, db: DbSession) -> PayoutsResponse:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            legacy = _LEGACY_PAYOUTS.get(plan_id)
            snapshot = plan.fee_policy_snapshot
            if (legacy is None or type(snapshot) is not dict
                    or "approval_proposal" in snapshot):
                PlanService.verify_approval_proposal(tx, plan)
                legacy = None
            if plan.state not in {"paying", "settled_with_exceptions", "settled"}:
                raise StateError("Plan has no payouts to reconcile", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["paying", "settled_with_exceptions", "settled"])
            batch = tx.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                              .order_by(PayoutBatch.attempt.desc()).limit(1))
            if batch is None:
                raise StateError("Payout provider batch is not yet known", code="payout_batch_unknown",
                                 current_state=plan.state, allowed_transitions=[])
            if legacy is not None:
                self._verify_legacy_payout_intent(tx, plan, batch, legacy)
            if batch.external_id is None:
                batch.status = "needs_attention"
                tx.commit()
                return self.get_payouts(plan_id, db)
            batch_id = batch.external_id
            attempt = batch.attempt
        try:
            detail = self.provider.get_payout_batch(batch_id)
        except Exception as exc:
            raise _provider_failure(exc) from exc
        if not isinstance(detail, BatchDetail) or detail.batch_id != batch_id:
            raise PaymentProviderError("Provider returned invalid payout detail", code="invalid_payout_response")
        if legacy is not None:
            self._reconcile_legacy_payout(plan_id, attempt, legacy, detail, db)
            return self.get_payouts(plan_id, db)
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            batch = tx.scalar(select(PayoutBatch).where(PayoutBatch.external_id == batch_id))
            operation = tx.scalar(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id, PaymentOperation.kind == "payout",
                PaymentOperation.attempt == attempt,
            ))
            latest = tx.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                               .order_by(PayoutBatch.attempt.desc()).limit(1))
            if (batch is None or operation is None or operation.payout_batch_id != batch.id
                    or plan.payout_attempt != attempt or latest is None or latest.id != batch.id):
                raise StateError("Payout reconciliation result is stale",
                                 code="payout_reconciliation_stale", current_state=plan.state,
                                 allowed_transitions=[])
            batch.status = map_batch_status(detail.status)
            if detail.fees_cents is not None:
                fees_were_unknown = batch.fees_cents is None
                batch.fees_cents = detail.fees_cents
                if fees_were_unknown:
                    append_ledger_event(tx, "PAYOUT_FEE_ACTUALIZED", {
                        "estimated_cents": batch.estimated_fees_cents,
                        "actual_cents": detail.fees_cents,
                    }, plan_id=plan_id, external_id=batch_id)
            rows = {item.sender_item_id: item for item in tx.scalars(
                select(PayoutItem).where(PayoutItem.batch_id == batch.id))}
            seen = set()
            for reported in detail.items:
                item = rows.get(reported.sender_item_id)
                if item is None or reported.sender_item_id in seen or reported.amount_cents != item.amount_cents:
                    raise PaymentProviderError("Payout item response does not match the intent",
                                               code="payout_item_mismatch")
                seen.add(reported.sender_item_id)
                status = map_item_status(reported.raw_status)
                if item.status in {"success", "failed", "returned"} or (
                    item.status == "unclaimed" and status != "returned"
                ):
                    continue
                if item.status != status or item.external_id != reported.external_id:
                    item.status, item.external_id = status, reported.external_id
                    item.failure_reason = reported.failure_reason if status == "failed" else None
                    item.updated_at = datetime.now(UTC)
                    append_ledger_event(tx, f"PAYOUT_ITEM_{status.upper()}", {
                        "sender_item_id": item.sender_item_id,
                        "upstream_key": item.upstream_key,
                        "amount_cents": item.amount_cents,
                    }, plan_id=plan_id, external_id=item.external_id)
            unsettled = list(tx.scalars(select(PayoutItem).where(
                PayoutItem.batch_id == batch.id,
                PayoutItem.status.in_(["pending", "needs_attention"]),
            )))
            if not unsettled and plan.state == "paying":
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)))
                exceptions = any(item.status in {"unclaimed", "failed", "returned"} for item in items)
                PlanService.transition(tx, plan_id, "paying",
                                       "settled_with_exceptions" if exceptions else "settled",
                                       "PLAN_SETTLED", {"exceptions": exceptions})
            else:
                tx.commit()
        return self.get_payouts(plan_id, db)

    @staticmethod
    def _verify_legacy_payout_intent(tx, plan, batch, expected) -> PayoutItem:
        batch_id, sender_item_id, external_item_id, amount_cents, original_state = expected
        snapshot = plan.fee_policy_snapshot
        batches = list(tx.scalars(select(PayoutBatch).where(PayoutBatch.plan_id == plan.id)))
        operation = tx.scalar(select(PaymentOperation).where(
            PaymentOperation.plan_id == plan.id, PaymentOperation.kind == "payout",
            PaymentOperation.attempt == 1))
        items = list(tx.scalars(select(PayoutItem).where(PayoutItem.plan_id == plan.id)))
        valid_item_states = {"success"} if original_state == "settled" else {"pending", "success"}
        if (type(snapshot) is not dict or "approval_proposal" in snapshot
                or plan.payout_attempt != 1 or plan.state not in {original_state, "settled"}
                or len(batches) != 1 or batches[0].id != batch.id
                or batch.plan_id != plan.id or batch.attempt != 1
                or batch.sender_batch_id != f"plan_{plan.id}_payout_1"
                or batch.external_id != batch_id or len(items) != 1
                or items[0].batch_id != batch.id
                or items[0].sender_item_id != sender_item_id
                or items[0].external_id != external_item_id
                or items[0].amount_cents != amount_cents
                or items[0].status not in valid_item_states
                or batch.status not in {"pending", "success"}
                or operation is None or operation.state != "confirmed"
                or operation.external_id != batch_id or operation.payout_batch_id != batch.id
                or operation.request_id != f"plan:{plan.id}:payout:1"):
            raise PaymentProviderError("Legacy payout records do not match the approved compatibility scope",
                                       code="legacy_payout_mismatch")
        return items[0]

    def _reconcile_legacy_payout(self, plan_id: str, attempt: int, expected,
                                 detail: BatchDetail, db: DbSession) -> None:
        batch_id, sender_item_id, external_item_id, amount_cents, _original_state = expected
        if (detail.batch_id != batch_id or map_batch_status(detail.status) != "success"
                or len(detail.items) != 1):
            raise PaymentProviderError("Fresh provider payout status does not match the legacy intent",
                                       code="legacy_payout_mismatch")
        reported = detail.items[0]
        if (reported.sender_item_id != sender_item_id or reported.external_id != external_item_id
                or reported.amount_cents != amount_cents or reported.currency != "USD"
                or map_item_status(reported.raw_status) != "success"
                or type(detail.fees_cents) is not int or detail.fees_cents < 0):
            raise PaymentProviderError("Fresh provider payout item does not match the legacy intent",
                                       code="legacy_payout_mismatch")

        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            batch = tx.scalar(select(PayoutBatch).where(
                PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
            if batch is None:
                raise PaymentProviderError("Legacy payout batch disappeared during reconciliation",
                                           code="legacy_payout_mismatch")
            item = self._verify_legacy_payout_intent(tx, plan, batch, expected)
            if batch.fees_cents is not None and batch.fees_cents != detail.fees_cents:
                raise PaymentProviderError("Fresh provider fee does not match the stored legacy payout fee",
                                           code="legacy_payout_mismatch")
            batch.status = "success"
            if batch.fees_cents is None:
                batch.fees_cents = detail.fees_cents
                append_ledger_event(tx, "PAYOUT_FEE_ACTUALIZED", {
                    "estimated_cents": batch.estimated_fees_cents,
                    "actual_cents": detail.fees_cents,
                }, plan_id=plan_id, external_id=batch_id)
            if item.status != "success":
                item.status = "success"
                item.external_id = external_item_id
                item.failure_reason = None
                item.updated_at = datetime.now(UTC)
                append_ledger_event(tx, "PAYOUT_ITEM_SUCCESS", {
                    "sender_item_id": item.sender_item_id,
                    "upstream_key": item.upstream_key,
                    "amount_cents": item.amount_cents,
                }, plan_id=plan_id, external_id=external_item_id)
            if plan.state == "paying":
                PlanService.transition(tx, plan_id, "paying", "settled", "PLAN_SETTLED",
                                       {"exceptions": False})
            else:
                tx.commit()

    @staticmethod
    def _batch_status(plan_id: str, attempt: int, db: DbSession) -> str:
        with _new_session(db) as tx:
            batch = tx.scalar(select(PayoutBatch).where(
                PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
            return batch.status

    def _require_measured_policy(self) -> None:
        if not self.policy.fees.measured or not self.policy.fees.supports_standard_us_usd:
            raise ValidationError("Payment operations require the supported standard US domestic USD fee policy",
                                  code="fee_policy_unmeasured")

    def get_payouts(self, plan_id: str, db: DbSession) -> PayoutsResponse:
        with _new_session(db) as tx:
            PlanService.get(tx, plan_id)
            batches = list(tx.scalars(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                                      .order_by(PayoutBatch.attempt)))
            responses = []
            all_items = [item for batch in batches for item in tx.scalars(
                select(PayoutItem).where(PayoutItem.batch_id == batch.id)
            )]
            keys = {item.upstream_key for item in all_items}
            recipient_ids = {item.maintainer_id for item in all_items if item.maintainer_id}
            legacy_by_key = {row.upstream_key: row.display_name for row in tx.scalars(
                select(Maintainer).where(Maintainer.upstream_key.in_(keys))
            )} if keys else {}
            display_by_id = {row.id: row.display_name for row in tx.scalars(
                select(MaintainerRecipient).where(MaintainerRecipient.id.in_(recipient_ids))
            )} if recipient_ids else {}
            display_by_id.update({row.id: row.display_name for row in tx.scalars(
                select(Maintainer).where(Maintainer.id.in_(recipient_ids))
            )} if recipient_ids else {})
            for batch in batches:
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)
                                        .order_by(PayoutItem.sender_item_id)))
                responses.append(BatchResponse(
                    id=batch.external_id or batch.sender_batch_id, attempt=batch.attempt,
                    status=batch.status, fees_cents=batch.fees_cents,
                    items=[ItemResponse(sender_item_id=item.sender_item_id,
                                        upstream_key=item.upstream_key,
                                        maintainer_id=item.maintainer_id,
                                        recipient_display=display_by_id.get(
                                            item.maintainer_id,
                                            legacy_by_key.get(item.upstream_key, item.upstream_key),
                                        ),
                                        amount_cents=item.amount_cents,
                                        status=item.status, failure_reason=item.failure_reason,
                                        updated_at=item.updated_at)
                           for item in items],
                ))
            return PayoutsResponse(batches=responses)

    def cancel(self, plan_id: str, db: DbSession) -> None:
        with _new_session(db) as tx:
            _begin_immediate(tx)
            plan = PlanService.get(tx, plan_id)
            PlanService.verify_approval_proposal(tx, plan)
            if plan.state not in {"draft", "funding"} or plan.capture_gross_cents is not None:
                raise StateError("Plan cannot be cancelled", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["cancelled"])
            operations = list(tx.scalars(select(PaymentOperation).where(
                PaymentOperation.plan_id == plan_id,
                PaymentOperation.kind.in_(["order", "capture"]),
            )))
            blocked_capture_states = {"intent", "submitting", "unknown", "confirmed", "resolved"}
            blocked_order_states = {"intent", "submitting", "unknown"}
            if any(operation.kind == "capture" and operation.state in blocked_capture_states
                   for operation in operations):
                raise StateError("Plan has an active or unresolved capture", code="capture_in_progress",
                                 current_state=plan.state, allowed_transitions=[])
            if any(operation.kind == "order" and operation.state in blocked_order_states
                   for operation in operations):
                raise StateError("Plan has an active or unresolved order", code="order_outcome_unknown",
                                 current_state=plan.state, allowed_transitions=[])
            if plan.state == "funding" and not plan.order_id:
                raise StateError("Order outcome must be resolved before cancellation",
                                 code="order_outcome_unknown", current_state=plan.state,
                                 allowed_transitions=[])
            plan.approval_url = None
            PlanService.transition(tx, plan_id, plan.state, "cancelled", "PLAN_CANCELLED", {})
