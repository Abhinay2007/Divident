"""Payment orchestration; only this service calls PaymentsProvider."""

from datetime import UTC, datetime
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..allocation.policy import load_allocation_policy
from ..errors import (InvariantViolation, NotFoundError, PaymentProviderError,
                      StateError, ValidationError)
from ..ledger import append as append_ledger_event
from ..models import (LedgerEvent, Maintainer, PayoutBatch, PayoutItem, Plan,
                      PlanLine)
from ..schemas import (ApproveResponse, BatchResponse, CaptureDetail,
                       CaptureResponse, ItemResponse, PayoutResponse,
                       PayoutsResponse)
from ..payments.base import (BatchDetail, BatchResult, CaptureResult, OrderDetail,
                             OrderResult, PaymentsProvider, PayoutItemInput)
from ..payments.reconcile import map_batch_status, map_item_status
from .plan_service import PlanService

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _new_session(db: DbSession) -> DbSession:
    return DbSession(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def _provider_failure(exc: Exception) -> PaymentProviderError:
    if isinstance(exc, PaymentProviderError):
        return exc
    return PaymentProviderError("Payment provider call failed", code="provider_error")


class PaymentService:
    def __init__(self, provider: PaymentsProvider, public_base_url: str,
                 allocation_policy_path=None):
        self.provider = provider
        self.public_base_url = str(public_base_url).rstrip("/")
        self.policy = load_allocation_policy(allocation_policy_path) if allocation_policy_path is not None else load_allocation_policy()

    def approve_and_fund(self, plan_id: str, db: DbSession) -> ApproveResponse:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
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
            if plan.state == "funded" and plan.capture_gross_cents is not None:
                event = tx.scalar(select(LedgerEvent).where(
                    LedgerEvent.plan_id == plan_id, LedgerEvent.type == "ORDER_CAPTURED"
                ).order_by(LedgerEvent.created_at.desc()).limit(1))
                capture_id = event.payload.get("capture_id", "") if event else ""
                result = CaptureResult(capture_id, plan.capture_gross_cents, plan.capture_fee_cents,
                                       plan.capture_net_cents, "USD", "COMPLETED")
                return self._capture_response(result, plan.state)
            if plan.state != "funding" or not plan.order_id:
                raise StateError("Plan has no capturable order", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["funding"])
            if plan.charge_cents is None or plan.est_payout_fees_cents is None:
                raise ValidationError("Capture requirement is unavailable", code="charge_unavailable")
            reconcile_first = plan.capture_attempt > 0
            if plan.capture_attempt == 0:
                plan.capture_attempt = 1
                tx.commit()
            order_id, attempt = plan.order_id, plan.capture_attempt

        request_id = f"plan:{plan_id}:capture:{attempt}"
        if reconcile_first:
            try:
                order = self.provider.get_order(order_id)
            except Exception as exc:
                raise _provider_failure(exc) from exc
            if order.captures:
                capture = order.captures[0]
                with _new_session(db) as tx:
                    plan = PlanService.mark_captured(tx, plan_id, capture, self.policy.fees.tolerance_cents)
                    state = plan.state
                    tx.commit()
                return self._capture_response(capture, state)
        try:
            capture = self.provider.capture_order(order_id, request_id)
        except PaymentProviderError as exc:
            if exc.code not in {"ORDER_ALREADY_CAPTURED", "TRANSACTION_ALREADY_COMPLETED"}:
                raise
            try:
                order = self.provider.get_order(order_id)
            except Exception as recovery_exc:
                raise _provider_failure(recovery_exc) from recovery_exc
            if not isinstance(order, OrderDetail) or not order.captures:
                raise PaymentProviderError("Captured order has no recoverable capture", code="capture_recovery_failed") from exc
            capture = order.captures[0]
        except Exception as exc:
            raise _provider_failure(exc) from exc
        if not isinstance(capture, CaptureResult):
            raise PaymentProviderError("Provider returned an invalid capture", code="invalid_capture_response")
        with _new_session(db) as tx:
            plan = PlanService.mark_captured(tx, plan_id, capture, self.policy.fees.tolerance_cents)
            state = plan.state
            tx.commit()
        return self._capture_response(capture, state)

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
            plan = PlanService.get(tx, plan_id)
            latest = tx.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                               .order_by(PayoutBatch.attempt.desc()).limit(1))
            if plan.state == "paying" and latest is not None:
                batch = latest
                if batch.external_id:
                    return PayoutResponse(batch_id=batch.external_id,
                                          item_count=len(batch.items), state=batch.status)
                batch.status = "needs_attention"
                tx.commit()
                return PayoutResponse(batch_id=batch.sender_batch_id,
                                      item_count=len(batch.items), state=batch.status)
            else:
                expected_state = "settled_with_exceptions" if retry else "funded"
                if plan.state != expected_state:
                    raise StateError("Plan cannot dispatch payouts from its current state",
                                     code="invalid_plan_transition", current_state=plan.state,
                                     allowed_transitions=[expected_state])
                lines = list(tx.scalars(select(PlanLine).where(
                    PlanLine.plan_id == plan_id, PlanLine.payable_cents > 0
                ).order_by(PlanLine.upstream_key)))
                if retry:
                    if latest is None:
                        raise StateError("No payout batch is available to retry", code="no_failed_payout_items",
                                         current_state=plan.state, allowed_transitions=[])
                    failed = {item.upstream_key: item for item in tx.scalars(
                        select(PayoutItem).where(PayoutItem.batch_id == latest.id, PayoutItem.status == "failed"))}
                    lines = [line for line in lines if line.upstream_key in failed]
                    if not lines:
                        raise StateError("Only failed payout items can be retried", code="no_failed_payout_items",
                                         current_state=plan.state, allowed_transitions=[])
                if not lines:
                    raise StateError("Plan has no payable allocations", code="no_payable_allocations",
                                     current_state=plan.state, allowed_transitions=[])
                payees = {row.upstream_key: row for row in tx.scalars(select(Maintainer).where(
                    Maintainer.upstream_key.in_([line.upstream_key for line in lines]),
                    Maintainer.opted_in.is_(True),
                ))}
                for line in lines:
                    payee = payees.get(line.upstream_key)
                    if payee is None or not _EMAIL.fullmatch(payee.email):
                        raise InvariantViolation("Payable allocation has no valid opted-in recipient",
                                                 code="invalid_payout_recipient")
                attempt = plan.payout_attempt + 1
                sender_batch_id = f"plan_{plan_id}_payout_{attempt}"
                batch = PayoutBatch(plan_id=plan_id, attempt=attempt,
                                    sender_batch_id=sender_batch_id, status="submitting")
                tx.add(batch)
                tx.flush()
                items = []
                for index, line in enumerate(lines):
                    item = PayoutItem(
                        batch_id=batch.id, plan_id=plan_id, upstream_key=line.upstream_key,
                        sender_item_id=f"p{plan_id[:8]}-a{attempt}-{index:03d}",
                        recipient_email=payees[line.upstream_key].email,
                        amount_cents=line.payable_cents, status="pending",
                    )
                    tx.add(item)
                    items.append(item)
                plan.payout_attempt = attempt
                PlanService.transition(tx, plan_id, expected_state, "paying", "PAYOUT_INTENT", {
                    "attempt": attempt, "sender_batch_id": sender_batch_id,
                    "item_count": len(items),
                }, additional_events=[])
                # transition() committed the intent and all items atomically.
                batch = tx.get(PayoutBatch, batch.id)
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)
                                        .order_by(PayoutItem.sender_item_id)))
            sender_batch_id, attempt = batch.sender_batch_id, batch.attempt
            inputs = [PayoutItemInput(item.sender_item_id, item.recipient_email,
                                      item.amount_cents, "USD") for item in items]

        try:
            result = self.provider.create_payout_batch(
                sender_batch_id, f"plan:{plan_id}:payout:{attempt}", inputs,
            )
        except Exception as exc:
            failure = _provider_failure(exc)
            with _new_session(db) as tx:
                batch = tx.scalar(select(PayoutBatch).where(
                    PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
                if batch is not None:
                    if failure.detail.get("outcome_unknown", True):
                        batch.status = "needs_attention"
                        append_ledger_event(tx, "PAYOUT_OUTCOME_UNKNOWN", {
                            "sender_batch_id": sender_batch_id, "code": failure.code,
                        }, plan_id=plan_id, external_id=sender_batch_id)
                    else:
                        batch.status = "intent"
                    tx.commit()
            raise failure from exc
        if not isinstance(result, BatchResult) or not result.batch_id:
            raise PaymentProviderError("Provider returned an invalid payout batch", code="invalid_payout_response")

        with _new_session(db) as tx:
            batch = tx.scalar(select(PayoutBatch).where(
                PayoutBatch.plan_id == plan_id, PayoutBatch.attempt == attempt))
            if batch.external_id is None:
                batch.external_id = result.batch_id
                batch.status = map_batch_status(result.status)
                batch.fees_cents = result.fees_cents
                append_ledger_event(tx, "PAYOUT_BATCH_CREATED", {
                    "batch_id": result.batch_id, "status": batch.status,
                    "item_count": len(inputs),
                }, plan_id=plan_id, external_id=result.batch_id)
                estimate = tx.get(Plan, plan_id).est_payout_fees_cents
                append_ledger_event(tx, "PAYOUT_FEE_RECONCILED", {
                    "estimated_cents": estimate, "actual_cents": result.fees_cents,
                    "diff_cents": result.fees_cents - estimate if result.fees_cents is not None and estimate is not None else None,
                }, plan_id=plan_id, external_id=result.batch_id)
                tx.commit()
            return PayoutResponse(batch_id=batch.external_id, item_count=len(items), state=batch.status)

    def reconcile(self, plan_id: str, db: DbSession) -> PayoutsResponse:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            if plan.state not in {"paying", "settled_with_exceptions"}:
                raise StateError("Plan has no payouts to reconcile", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["paying", "settled_with_exceptions"])
            batch = tx.scalar(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                              .order_by(PayoutBatch.attempt.desc()).limit(1))
            if batch is None:
                raise StateError("Payout provider batch is not yet known", code="payout_batch_unknown",
                                 current_state=plan.state, allowed_transitions=[])
            if batch.external_id is None:
                batch.status = "needs_attention"
                tx.commit()
                return self.get_payouts(plan_id, db)
            batch_id = batch.external_id
        try:
            detail = self.provider.get_payout_batch(batch_id)
        except Exception as exc:
            raise _provider_failure(exc) from exc
        if not isinstance(detail, BatchDetail):
            raise PaymentProviderError("Provider returned invalid payout detail", code="invalid_payout_response")
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            batch = tx.scalar(select(PayoutBatch).where(PayoutBatch.external_id == batch_id))
            batch.status = map_batch_status(detail.status)
            if detail.fees_cents is not None:
                batch.fees_cents = detail.fees_cents
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

    def get_payouts(self, plan_id: str, db: DbSession) -> PayoutsResponse:
        with _new_session(db) as tx:
            PlanService.get(tx, plan_id)
            batches = list(tx.scalars(select(PayoutBatch).where(PayoutBatch.plan_id == plan_id)
                                      .order_by(PayoutBatch.attempt)))
            responses = []
            for batch in batches:
                items = list(tx.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)
                                        .order_by(PayoutItem.sender_item_id)))
                responses.append(BatchResponse(
                    id=batch.external_id or batch.sender_batch_id, attempt=batch.attempt,
                    status=batch.status, fees_cents=batch.fees_cents,
                    items=[ItemResponse(sender_item_id=item.sender_item_id,
                                        upstream_key=item.upstream_key,
                                        recipient_email=item.recipient_email,
                                        amount_cents=item.amount_cents,
                                        status=item.status, updated_at=item.updated_at)
                           for item in items],
                ))
            return PayoutsResponse(batches=responses)

    def cancel(self, plan_id: str, db: DbSession) -> None:
        with _new_session(db) as tx:
            plan = PlanService.get(tx, plan_id)
            if plan.state not in {"draft", "funding"} or plan.capture_gross_cents is not None:
                raise StateError("Plan cannot be cancelled", code="invalid_plan_transition",
                                 current_state=plan.state, allowed_transitions=["cancelled"])
            PlanService.transition(tx, plan_id, plan.state, "cancelled", "PLAN_CANCELLED", {})
