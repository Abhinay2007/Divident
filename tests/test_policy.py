from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from dividend.allocation import (
    AllocationPolicy,
    AllocationPolicyError,
    AllocationRules,
    FeePolicy,
    NeedFactorPolicy,
    load_allocation_policy,
)
from dividend.config import DEFAULT_POLICY_PATH


POLICY_YAML = """\
measured: false
fees:
  order_fee_pct: null
  order_fee_fixed_cents: null
  payout_fee_per_item_cents: null
  buffer_cents: null
  tolerance_cents: 5
allocation:
  max_binaries_cap: 50
  per_project_cap_pct: 15
  min_line_cents: 100
need_factor:
  archived: 0.5
  inactive_12m: 0.8
  committers_1: 1.5
  committers_2_5: 1.2
  committers_6_plus: 1.0
"""


def write_policy(tmp_path, text=POLICY_YAML):
    path = tmp_path / "allocation_policy.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_actual_policy_loads_documented_values_and_null_unknown_fees():
    policy = load_allocation_policy(DEFAULT_POLICY_PATH)

    assert policy.fees.measured is False
    assert policy.fees.order_fee_pct is None
    assert policy.fees.order_fee_fixed_cents is None
    assert policy.fees.payout_fee_per_item_cents is None
    assert policy.fees.buffer_cents is None
    assert policy.fees.tolerance_cents == 5
    assert policy.allocation == AllocationRules(50, Decimal("15"), 100)
    assert policy.need_factors == NeedFactorPolicy(
        Decimal("0.5"), Decimal("0.8"), Decimal("1.5"),
        Decimal("1.2"), Decimal("1.0"),
    )


def test_decimal_yaml_scalars_are_parsed_directly_as_decimal(tmp_path):
    policy = load_allocation_policy(write_policy(
        tmp_path,
        POLICY_YAML.replace("per_project_cap_pct: 15", "per_project_cap_pct: 15.25")
        .replace("order_fee_pct: null", "order_fee_pct: 2.75"),
    ))
    assert type(policy.allocation.per_project_cap_pct) is Decimal
    assert policy.allocation.per_project_cap_pct == Decimal("15.25")
    assert type(policy.fees.order_fee_pct) is Decimal
    assert policy.fees.order_fee_pct == Decimal("2.75")


def test_policy_models_are_frozen_and_reject_float_decimal_values():
    policy = load_allocation_policy(DEFAULT_POLICY_PATH)
    with pytest.raises(FrozenInstanceError):
        policy.fees.measured = True
    with pytest.raises((TypeError, ValueError)):
        AllocationRules(1, 15.0, 1)
    with pytest.raises((TypeError, ValueError)):
        NeedFactorPolicy(0.5, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))


def test_missing_required_policy_key_is_rejected_with_path(tmp_path):
    text = POLICY_YAML.replace("  min_line_cents: 100\n", "")
    with pytest.raises(AllocationPolicyError, match="allocation.*min_line_cents"):
        load_allocation_policy(write_policy(tmp_path, text))


def test_unknown_policy_key_is_rejected_with_path(tmp_path):
    text = POLICY_YAML.replace("  max_binaries_cap: 50\n", "  max_binaries_cap: 50\n  extra: 1\n")
    with pytest.raises(AllocationPolicyError, match="allocation.*extra"):
        load_allocation_policy(write_policy(tmp_path, text))


@pytest.mark.parametrize(
    "old,new,field",
    [
        ("per_project_cap_pct: 15", "per_project_cap_pct: 0", "per_project_cap_pct"),
        ("per_project_cap_pct: 15", "per_project_cap_pct: 100.01", "per_project_cap_pct"),
        ("archived: 0.5", "archived: 0", "need_factor.archived"),
        ("archived: 0.5", "archived: -0.1", "need_factor.archived"),
        ("archived: 0.5", "archived: .nan", "need_factor.archived"),
        ("archived: 0.5", "archived: .inf", "need_factor.archived"),
        ("order_fee_pct: null", "order_fee_pct: 100", "order_fee_pct"),
        ("order_fee_pct: null", "order_fee_pct: -0.1", "order_fee_pct"),
    ],
)
def test_invalid_decimal_ranges_are_rejected(tmp_path, old, new, field):
    with pytest.raises(AllocationPolicyError, match=field):
        load_allocation_policy(write_policy(tmp_path, POLICY_YAML.replace(old, new)))


@pytest.mark.parametrize(
    "old,new,field",
    [
        ("max_binaries_cap: 50", "max_binaries_cap: true", "max_binaries_cap"),
        ("max_binaries_cap: 50", "max_binaries_cap: 0", "max_binaries_cap"),
        ("min_line_cents: 100", "min_line_cents: 1.0", "min_line_cents"),
        ("tolerance_cents: 5", "tolerance_cents: -1", "tolerance_cents"),
        ("tolerance_cents: 5", "tolerance_cents: false", "tolerance_cents"),
        ("order_fee_fixed_cents: null", "order_fee_fixed_cents: -1", "order_fee_fixed_cents"),
        ("payout_fee_per_item_cents: null", "payout_fee_per_item_cents: -1", "payout_fee_per_item_cents"),
        ("buffer_cents: null", "buffer_cents: -1", "buffer_cents"),
    ],
)
def test_invalid_integer_policy_values_are_rejected(tmp_path, old, new, field):
    with pytest.raises(AllocationPolicyError, match=field):
        load_allocation_policy(write_policy(tmp_path, POLICY_YAML.replace(old, new)))


def test_measured_policy_requires_all_fee_formula_parameters(tmp_path):
    text = POLICY_YAML.replace("measured: false", "measured: true")
    with pytest.raises(AllocationPolicyError, match="measured fee policy requires"):
        load_allocation_policy(write_policy(tmp_path, text))


def test_measured_policy_accepts_complete_fee_formula_parameters(tmp_path):
    text = POLICY_YAML.replace("measured: false", "measured: true")
    text = text.replace("order_fee_pct: null", "order_fee_pct: 2.5")
    text = text.replace("order_fee_fixed_cents: null", "order_fee_fixed_cents: 20")
    text = text.replace("payout_fee_per_item_cents: null", "payout_fee_per_item_cents: 25")
    text = text.replace("buffer_cents: null", "buffer_cents: 10")
    policy = load_allocation_policy(write_policy(tmp_path, text))
    assert policy.fees.measured is True
    assert policy.fees.order_fee_pct == Decimal("2.5")
    assert policy.fees.order_fee_fixed_cents == 20


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    text = POLICY_YAML.replace("measured: false\n", "measured: false\nmeasured: false\n")
    with pytest.raises(AllocationPolicyError, match="(?s)invalid YAML.*duplicate key"):
        load_allocation_policy(write_policy(tmp_path, text))


def test_direct_policy_models_reject_boolean_in_integer_fields():
    with pytest.raises(TypeError):
        AllocationRules(True, Decimal("15"), 1)
    with pytest.raises(TypeError):
        FeePolicy(False, None, True, None, None, 0)


def test_top_level_policy_requires_all_sections(tmp_path):
    text = POLICY_YAML.replace("need_factor:\n", "")
    with pytest.raises(AllocationPolicyError, match="policy missing required key.*need_factor"):
        load_allocation_policy(write_policy(tmp_path, text))


@pytest.mark.parametrize("value", ["1.0e+10000", "1.0e-10000"])
def test_decimal_exponent_boundaries_are_accepted(tmp_path, value):
    policy = load_allocation_policy(write_policy(
        tmp_path, POLICY_YAML.replace("archived: 0.5", f"archived: {value}"),
    ))
    assert policy.need_factors.archived.is_finite()


@pytest.mark.parametrize("value", ["1.0e+10001", "1.0e-10001", "1.0e+100000000", "1.0e-100000000"])
def test_decimal_exponents_outside_bounds_are_rejected_without_expansion(tmp_path, value):
    with pytest.raises(AllocationPolicyError, match="exponent must be between"):
        load_allocation_policy(write_policy(
            tmp_path, POLICY_YAML.replace("archived: 0.5", f"archived: {value}"),
        ))


def test_significant_digit_boundary_and_equivalent_representations(tmp_path):
    within = "0." + "1" * 1000
    policy = load_allocation_policy(write_policy(
        tmp_path, POLICY_YAML.replace("archived: 0.5", f"archived: {within}"),
    ))
    assert len(policy.need_factors.archived.as_tuple().digits) == 1000
    outside = "1." + "1" * 1000
    with pytest.raises(AllocationPolicyError, match="1000 significant digits"):
        load_allocation_policy(write_policy(
            tmp_path, POLICY_YAML.replace("archived: 0.5", f"archived: {outside}"),
        ))

    first = load_allocation_policy(write_policy(
        tmp_path, POLICY_YAML.replace("archived: 0.5", "archived: 1.2300"),
    ))
    second = load_allocation_policy(write_policy(
        tmp_path, POLICY_YAML.replace("archived: 0.5", "archived: 1.23"),
    ))
    assert first.need_factors.archived == second.need_factors.archived
    assert first.need_factors.archived.as_tuple() == second.need_factors.archived.as_tuple()


def test_equivalent_exponent_boundary_representations_have_same_acceptance(tmp_path):
    accepted = ["archived: 1.0e+10000", "archived: 10.0e+9999"]
    values = [
        load_allocation_policy(write_policy(
            tmp_path, POLICY_YAML.replace("archived: 0.5", text),
        )).need_factors.archived.as_tuple()
        for text in accepted
    ]
    assert values[0] == values[1]
    for value in ("archived: 1.0e+10001", "archived: 10.0e+10000"):
        with pytest.raises(AllocationPolicyError, match="exponent must be between"):
            load_allocation_policy(write_policy(
                tmp_path, POLICY_YAML.replace("archived: 0.5", value),
            ))


def test_direct_policy_models_accept_canonical_exponent_boundaries():
    lower = Decimal((0, (1,), -10_000))
    upper = Decimal((0, (1,), 10_000))

    assert AllocationRules(1, lower, 1).per_project_cap_pct == lower
    assert NeedFactorPolicy(*(lower for _ in range(5))).archived == lower
    assert NeedFactorPolicy(*(upper for _ in range(5))).archived == upper
    assert FeePolicy(False, lower, None, None, None, 0).order_fee_pct == lower


@pytest.mark.parametrize("exponent", [-10_001, 10_001])
def test_direct_policy_models_reject_exponents_outside_canonical_bounds(exponent):
    invalid = Decimal((0, (1,), exponent))
    with pytest.raises(ValueError, match="exponent must be between"):
        AllocationRules(1, invalid, 1)
    with pytest.raises(ValueError, match="exponent must be between"):
        NeedFactorPolicy(*(invalid for _ in range(5)))
    with pytest.raises(ValueError, match="exponent must be between"):
        FeePolicy(False, invalid, None, None, None, 0)


def test_direct_policy_models_enforce_significant_digit_boundary():
    at_limit_factor = Decimal("0." + "1" * 1_000)
    over_limit_factor = Decimal("0." + "1" * 1_001)
    at_limit_pct = Decimal("1." + "0" * 998 + "1")
    over_limit_pct = Decimal("1." + "0" * 999 + "1")

    assert NeedFactorPolicy(*(at_limit_factor for _ in range(5))).archived == at_limit_factor
    with pytest.raises(ValueError, match="1000 significant digits"):
        NeedFactorPolicy(*(over_limit_factor for _ in range(5)))

    assert AllocationRules(1, at_limit_pct, 1).per_project_cap_pct == at_limit_pct
    with pytest.raises(ValueError, match="1000 significant digits"):
        AllocationRules(1, over_limit_pct, 1)

    assert FeePolicy(False, at_limit_pct, None, None, None, 0).order_fee_pct == at_limit_pct
    with pytest.raises(ValueError, match="1000 significant digits"):
        FeePolicy(False, over_limit_pct, None, None, None, 0)


def test_direct_policy_models_canonicalize_equivalent_decimal_representations():
    cap_with_zeros = AllocationRules(1, Decimal("1.2300"), 1)
    cap_canonical = AllocationRules(1, Decimal("1.23"), 1)
    factors_with_zeros = NeedFactorPolicy(*(Decimal("1.2300") for _ in range(5)))
    factors_canonical = NeedFactorPolicy(*(Decimal("1.23") for _ in range(5)))
    fee_with_zeros = FeePolicy(False, Decimal("2.5000"), None, None, None, 0)
    fee_canonical = FeePolicy(False, Decimal("2.5"), None, None, None, 0)

    assert cap_with_zeros.per_project_cap_pct.as_tuple() == cap_canonical.per_project_cap_pct.as_tuple()
    assert factors_with_zeros.archived.as_tuple() == factors_canonical.archived.as_tuple()
    assert fee_with_zeros.order_fee_pct.as_tuple() == fee_canonical.order_fee_pct.as_tuple()
