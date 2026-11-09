from dataclasses import replace
from decimal import Decimal

import pytest

from dividend.allocation.policy import load_allocation_policy
from dividend.config import DEFAULT_POLICY_PATH
from dividend.payments.fees import (
    calculate_gross_charge_cents,
    estimate_incoming_fee_cents,
    estimate_payout_fee_cents,
)


@pytest.mark.parametrize("gross,expected", [
    (100, 52), (1_000, 84), (5_000, 224), (10_000, 398),
])
def test_standard_incoming_fee_examples(gross, expected):
    assert estimate_incoming_fee_cents(gross) == expected


def test_incoming_fee_rounds_half_up_to_cents():
    # $50.00 × 3.49% is exactly $1.745; add the $0.49 fixed fee after rounding.
    assert estimate_incoming_fee_cents(5_000) == 224
    assert estimate_incoming_fee_cents(5_001) == 224


@pytest.mark.parametrize("items,expected", [(1, 25), (2, 50), (5, 125)])
def test_payout_fee_estimate_is_per_item(items, expected):
    assert estimate_payout_fee_cents(items) == expected


def test_gross_charge_covers_net_target_without_float_arithmetic():
    gross, estimated_fee = calculate_gross_charge_cents(30_000, 50)
    assert gross - estimated_fee >= 30_050
    assert gross - 1 - estimate_incoming_fee_cents(gross - 1) < 30_050


@pytest.mark.parametrize("change", [
    {"merchant_market": "CA"}, {"payer_market_assumption": "CA"},
    {"recipient_market": "CA"}, {"currency": "CAD"},
    {"checkout_method": "pay_later"}, {"payout_method": "other"},
    {"pricing_tier": "negotiated"}, {"currency_conversion": True},
    {"micropayments": True}, {"charity_pricing": True},
    {"negotiated_rates": True},
])
def test_unsupported_fee_scope_is_not_authorized(change):
    policy = load_allocation_policy(DEFAULT_POLICY_PATH)
    assert replace(policy.fees, **change).supports_standard_us_usd is False


def test_plan_calculation_inputs_are_integer_cents():
    assert estimate_incoming_fee_cents(0) == 49
    with pytest.raises(ValueError):
        estimate_incoming_fee_cents(Decimal("1.00"))
