from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from dividend.schemas import (
    ApproveResponse, BatchResponse, CaptureDetail, CaptureResponse, ConfigResponse,
    FeeParams, HealthResponse, ItemResponse, LedgerEventResponse, LedgerResponse,
    MoneySummary, PlanBudgetPatchRequest, PlanCreateRequest, PlanLineResponse,
    PlanResponse, ProjectResponse, ProjectsResponse, PayoutResponse, PayoutsResponse,
    ResolveLiveRequest, ResolveLiveResponse, SessionCreateRequest, SessionResponse,
)


def report():
    return {"schema": 1, "origin": "demo", "binary_count": 2, "source_count": 1, "sources": {"src": ["bin", "bin-dev"]}}


@pytest.mark.parametrize("mode,payload", [("demo", None), ("paste", "src1\nsrc2"), ("upload", report())])
def test_session_create_valid_modes(mode, payload):
    assert SessionCreateRequest(mode=mode, payload=payload).mode == mode


@pytest.mark.parametrize("mode,payload", [("invalid", None), ("demo", {}), ("paste", {"source": "x"}), ("upload", {"sources": {}})])
def test_session_create_rejects_invalid_modes_and_payloads(mode, payload):
    with pytest.raises(ValidationError):
        SessionCreateRequest(mode=mode, payload=payload)


def test_upload_matches_scanner_shape_and_source_limit():
    assert SessionCreateRequest(mode="upload", payload=report()).payload == report()
    with pytest.raises(ValidationError):
        SessionCreateRequest(mode="upload", payload={**report(), "sources": {"Bad": ["bin"]}})
    over_limit = {f"src{i}": [f"bin{i}"] for i in range(5001)}
    with pytest.raises(ValidationError, match="5000"):
        SessionCreateRequest(mode="upload", payload={**report(), "source_count": 5001, "sources": over_limit})
    with pytest.raises(ValidationError, match="source_count"):
        SessionCreateRequest(mode="upload", payload={**report(), "source_count": 2})
    with pytest.raises(ValidationError, match="binary_count"):
        SessionCreateRequest(mode="upload", payload={**report(), "binary_count": 3})


@pytest.mark.parametrize("value", [99, 1_000_001, True, 100.0])
def test_plan_create_rejects_out_of_range_or_non_integer_budget(value):
    with pytest.raises(ValidationError):
        PlanCreateRequest(budget_cents=value, evaluation_date=date(2026, 11, 6))


def test_budget_boundaries_and_patch_contract():
    assert PlanCreateRequest(budget_cents=100, evaluation_date=date(2026, 11, 6)).budget_cents == 100
    assert PlanCreateRequest(budget_cents=1_000_000, evaluation_date=date(2026, 11, 6)).budget_cents == 1_000_000
    assert PlanBudgetPatchRequest(budget_cents=100, evaluation_date=date(2026, 11, 6)).budget_cents == 100
    with pytest.raises(ValidationError):
        PlanBudgetPatchRequest(budget_cents=-1, evaluation_date=date(2026, 11, 6))


def test_live_resolve_source_validation_and_count():
    assert ResolveLiveRequest(source_names=["libfoo", "foo+bar.1-2"]).source_names[0] == "libfoo"
    for names in (["Bad"], ["foo_bar"]):
        with pytest.raises(ValidationError):
            ResolveLiveRequest(source_names=names)
    with pytest.raises(ValidationError):
        ResolveLiveRequest(source_names=["x"] * 5001)


def test_response_models_serialize_and_integer_cent_fields_are_strict():
    now = datetime.now(UTC)
    money = MoneySummary(charged_now_cents=100, pledged_cents=20, fee_estimate_cents=3)
    capture = CaptureDetail(capture_id="c", gross_cents=100, fee_cents=2, net_cents=98, currency="USD", status="COMPLETED")
    item = ItemResponse(sender_item_id="i", upstream_key="a/b", recipient_email="a@example.test", amount_cents=10, status="success", updated_at=now)
    batch = BatchResponse(id="b", attempt=1, status="success", fees_cents=1, items=[item])
    assert CaptureResponse(capture=capture, state="funded").model_dump()["capture"]["gross_cents"] == 100
    assert PayoutsResponse(batches=[batch]).model_dump()["batches"][0]["items"][0]["amount_cents"] == 10
    for cls, fields in ((MoneySummary, {"charged_now_cents": 1.0, "pledged_cents": 0, "fee_estimate_cents": 0}), (FeeParams, {"order_fee_pct": None, "order_fee_fixed_cents": None, "payout_fee_per_item_cents": None, "buffer_cents": None, "tolerance_cents": 1.0})):
        with pytest.raises(ValidationError):
            cls(**fields)


def test_all_response_contracts_construct_with_documented_fields():
    project = ProjectResponse(upstream_key="a/b", display_name="A", repository_url=None, binary_count=1, source_names=["src"], need_signals={}, payability="payable", reason_summary="reason")
    line = PlanLineResponse(upstream_key="a/b", display_name="A", amount_cents=100, payable_cents=100, reserved_cents=0, payability="payable", reason="reason", facts_snapshot={})
    plan = PlanResponse(id="p", session_id="s", state="draft", budget_cents=100, payable_total_cents=100, reserved_total_cents=0, est_payout_fees_cents=0, charge_cents=100, order_id=None, order_attempt=0, capture_attempt=0, payout_attempt=0, capture_gross_cents=None, capture_fee_cents=None, capture_net_cents=None, failure_reason=None, frozen_at=None, lines=[line], money_summary=MoneySummary(charged_now_cents=100, pledged_cents=0, fee_estimate_cents=0))
    assert ProjectsResponse(projects=[project], unmapped=[], no_upstream=[]).projects
    assert PlanResponse.model_validate(plan.model_dump()).id == "p"
    unavailable = PlanResponse(
        id="unknown-fees", session_id="s", state="draft", budget_cents=100,
        payable_total_cents=100, reserved_total_cents=0,
        est_payout_fees_cents=None, charge_cents=None, order_id=None,
        order_attempt=0, capture_attempt=0, payout_attempt=0,
        capture_gross_cents=None, capture_fee_cents=None, capture_net_cents=None,
        failure_reason=None, frozen_at=None, lines=[line],
        money_summary=MoneySummary(charged_now_cents=None, pledged_cents=0, fee_estimate_cents=None),
    )
    assert unavailable.charge_cents is None
    legacy_line = PlanLineResponse(
        upstream_key="legacy/repo", display_name="Legacy", amount_cents=100,
        payable_cents=0, reserved_cents=100, payability="reserved",
        reason="legacy line", facts_snapshot=None,
    )
    assert legacy_line.facts_snapshot is None
    assert ApproveResponse(order_id="o", approval_url="https://example.test").order_id == "o"
    assert PayoutResponse(batch_id="b", item_count=1, state="paying").item_count == 1
    assert LedgerResponse(events=[LedgerEventResponse(id="e", type="T", plan_id=None, session_id=None, external_id=None, payload={}, created_at=datetime.now(UTC))], next_cursor=None).events
    assert HealthResponse(db="ok", provider_mode="fake", fixtures_loaded=True).db == "ok"
    fees = FeeParams(order_fee_pct=None, order_fee_fixed_cents=None, payout_fee_per_item_cents=None, buffer_cents=None, tolerance_cents=0)
    assert ConfigResponse(budget_min_cents=100, budget_max_cents=1_000_000, fee_params=fees, paypal_env="sandbox", sandbox=True, llm_live_enabled=False, cold_start_seconds=0)
    assert ResolveLiveResponse(resolved=1, unmapped_remaining=0, cached=False).resolved == 1


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        SessionCreateRequest(mode="demo", extra="x")
