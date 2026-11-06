from datetime import UTC, date, datetime
from decimal import Decimal, Overflow, getcontext, setcontext

import pytest

from dividend.allocation import (
    AllocationError,
    AllocationInvariantError,
    AllocationPolicy,
    AllocationRules,
    FeePolicy,
    FactsSnapshot,
    NeedFactorPolicy,
    NormalizedFactsSnapshot,
    ProjectFacts,
    ProjectInput,
    ResolutionSnapshot,
    SourceFactsSnapshot,
    allocate,
)
from dividend.allocation.engine import _rank_exact_remainders, _round_shares
import dividend.allocation.engine as allocation_engine


def make_policy(*, cap_pct=Decimal("100"), minimum=100, factors=None, measured=False, payout_fee=None):
    factors = factors or {}
    return AllocationPolicy(
        AllocationRules(50, cap_pct, minimum),
        NeedFactorPolicy(
            archived=factors.get("archived", Decimal("1")),
            inactive_12m=factors.get("inactive_12m", Decimal("1")),
            committers_1=factors.get("committers_1", Decimal("1")),
            committers_2_5=factors.get("committers_2_5", Decimal("1")),
            committers_6_plus=factors.get("committers_6_plus", Decimal("1")),
        ),
        FeePolicy(measured, Decimal("0") if measured else None, 0 if measured else None,
                  payout_fee if measured else None, 0 if measured else None, 0),
    )


def project(key, *, binary_count=1, archived=None, last_commit=None, committers=None,
            partial=False, funding_yml=None, opted_in=True):
    normalized = NormalizedFactsSnapshot(binary_count, archived, last_commit, committers, partial, funding_yml)
    source = SourceFactsSnapshot(
        "src-" + key, None, None, None, None, partial,
        ResolutionSnapshot("none", None, None, False, False, datetime(2026, 1, 1, tzinfo=UTC)),
    )
    snapshot = FactsSnapshot((source,), normalized)
    facts = ProjectFacts(archived, last_commit, committers, partial, funding_yml, snapshot)
    return ProjectInput(key, key, binary_count, facts, opted_in)


def allocation(projects, budget, policy=None, on=date(2026, 11, 6)):
    return allocate(projects, budget, policy or make_policy(), on)


def test_all_committer_buckets_zero_and_unknown():
    policy = make_policy(factors={
        "committers_1": Decimal("1.5"),
        "committers_2_5": Decimal("1.2"),
        "committers_6_plus": Decimal("0.8"),
    })
    results = [
        allocation([project("zero", committers=0)], 100, policy).lines[0].weight,
        allocation([project("unknown", committers=None)], 100, policy).lines[0].weight,
        allocation([project("one", committers=1)], 100, policy).lines[0].weight,
        allocation([project("few", committers=3)], 100, policy).lines[0].weight,
        allocation([project("many", committers=6)], 100, policy).lines[0].weight,
    ]
    assert results == [Decimal("1"), Decimal("1"), Decimal("1.5"), Decimal("1.2"), Decimal("0.8")]


def test_archived_inactive_multiplication_and_neutral_signals():
    policy = make_policy(factors={"archived": Decimal("0.5"), "inactive_12m": Decimal("0.8")})
    result = allocation([project("a", archived=True, last_commit=date(2025, 11, 6), partial=True)], 100, policy)
    assert result.lines[0].weight == Decimal("0.40")
    assert result.lines[0].facts_snapshot.normalized.partial is True
    neutral = allocation([project("b", last_commit=None, funding_yml=True)], 100, policy)
    assert neutral.lines[0].weight == Decimal("1")


def test_reason_lists_capped_usage_each_factor_and_combined_factor_deterministically():
    policy = make_policy(factors={
        "archived": Decimal("0.5"),
        "inactive_12m": Decimal("0.8"),
        "committers_1": Decimal("1.5"),
    })
    source = project("reason", binary_count=12, archived=True,
                     last_commit=date(2025, 11, 6), committers=1,
                     partial=True, funding_yml=True)
    first = allocation([source], 100, policy).lines[0].reason
    second = allocation([source], 100, policy).lines[0].reason
    assert first == second
    assert "Capped binary usage: min(12, 50) = 12" in first
    assert "archived factor 0.5" in first
    assert "inactive_12m factor 0.8" in first
    assert "committers_1 factor 1.5" in first
    assert "Combined need factor: 0.600" in first
    assert "funding_yml" not in first and "partial" not in first


def test_reason_reports_actual_binary_cap_application():
    policy = AllocationPolicy(
        AllocationRules(2, Decimal("100"), 1),
        NeedFactorPolicy(*(Decimal("1") for _ in range(5))),
        FeePolicy(False, None, None, None, None, 0),
    )
    reason = allocation([project("capped", binary_count=10)], 1, policy).lines[0].reason
    assert "Capped binary usage: min(10, 2) = 2" in reason


@pytest.mark.parametrize("exponent", [10_000, -10_000])
def test_derived_weight_product_stays_inside_verified_envelope(exponent):
    maximum_factor = Decimal((0, (9,) * 1000, exponent))
    policy = AllocationPolicy(
        AllocationRules(50, Decimal("100"), 1),
        NeedFactorPolicy(*(maximum_factor for _ in range(5))),
        FeePolicy(False, None, None, None, None, 0),
    )
    facts_project = project(
        "derived", archived=True,
        last_commit=date(2025, 11, 6), committers=1,
    )
    weight, need_factor, _, _ = allocation_engine._derive_weight(
        facts_project, policy, date(2026, 11, 6),
    )
    assert weight.is_finite() and weight > 0
    assert need_factor.is_finite() and need_factor > 0
    assert abs(need_factor.as_tuple().exponent) <= 30_000
    assert len(need_factor.as_tuple().digits) <= 3_000
    assert abs(weight.as_tuple().exponent) <= 30_000


def test_derived_weight_envelope_scales_with_unbounded_integer_usage_without_truncation():
    usage = 10**5000
    policy = AllocationPolicy(
        AllocationRules(usage, Decimal("100"), 1),
        NeedFactorPolicy(*(Decimal("1") for _ in range(5))),
        FeePolicy(False, None, None, None, None, 0),
    )
    result = allocation([project("wide-int", binary_count=usage)], 100, policy)
    assert result.lines[0].weight == Decimal(usage)
    assert result.lines[0].amount_cents == 100


def test_weight_derivation_decimal_arithmetic_failure_becomes_allocation_error(monkeypatch):
    def overflow(_precision):
        raise Overflow("simulated Decimal overflow")

    monkeypatch.setattr(allocation_engine, "_private_context", overflow)
    with pytest.raises(AllocationError, match="weight derivation failed"):
        allocation([project("overflow")], 100)


def test_allocation_path_has_no_precision_refinement_certifier():
    assert not hasattr(allocation_engine, "_certify_decimal_sweep")


@pytest.mark.parametrize(
    "evaluation,expected_cutoff",
    [(date(2025, 2, 28), date(2024, 2, 28)), (date(2024, 2, 29), date(2023, 2, 28)),
     (date(2025, 3, 31), date(2024, 3, 31))],
)
def test_inclusive_calendar_year_cutoff(evaluation, expected_cutoff):
    policy = make_policy(factors={"inactive_12m": Decimal("2")})
    assert allocation([project("old", last_commit=expected_cutoff)], 100, policy, evaluation).lines[0].weight == Decimal("2")
    assert allocation([project("new", last_commit=date.fromordinal(expected_cutoff.toordinal() + 1))], 100, policy, evaluation).lines[0].weight == Decimal("1")


def test_future_commit_is_rejected():
    with pytest.raises(AllocationError, match="future"):
        allocation([project("future", last_commit=date(2026, 11, 7))], 100)


def test_usage_cap_and_weight_multiplication_are_decimal_and_deterministic():
    policy = AllocationPolicy(
        AllocationRules(3, Decimal("100"), 1),
        NeedFactorPolicy(*(Decimal("1.1") for _ in range(5))),
        FeePolicy(False, None, None, None, None, 0),
    )
    line = allocation([project("x", binary_count=100, archived=True)], 1, policy).lines[0]
    assert line.weight == Decimal("3.3")


def test_duplicate_keys_and_empty_inputs_are_rejected():
    p = project("same")
    with pytest.raises(AllocationError, match="duplicate"):
        allocation([p, p], 100)
    with pytest.raises(AllocationError, match="at least one"):
        allocation([], 0)


def test_feasibility_active_count_and_weight_rank_then_key_tie():
    policy = make_policy(cap_pct=Decimal("60"), minimum=300)
    projects = [project("z", binary_count=1), project("b", binary_count=5), project("a", binary_count=5), project("c", binary_count=2)]
    result = allocation(projects, 1000, policy)
    assert [line.upstream_key for line in result.lines] == ["a", "b", "c"]
    assert sum(line.amount_cents for line in result.lines) == 1000


@pytest.mark.parametrize("budget,cap_pct,minimum", [(100, Decimal("50"), 100), (250, Decimal("20"), 100)])
def test_cap_below_minimum_or_zero_is_infeasible(budget, cap_pct, minimum):
    with pytest.raises(AllocationError, match="cap"):
        allocation([project("x")], budget, make_policy(cap_pct=cap_pct, minimum=minimum))


def test_percentage_cap_uses_decimal_floor():
    policy = make_policy(cap_pct=Decimal("33.33"), minimum=1)
    result = allocation([project("a", binary_count=1), project("b", binary_count=2), project("c", binary_count=3), project("d", binary_count=4)], 101, policy)
    assert all(line.amount_cents <= 33 for line in result.lines)
    assert sum(line.amount_cents for line in result.lines) == 101


def test_bounded_sweep_activates_cap_and_minimum_without_iteration_limit():
    projects = [project("a", binary_count=1), project("b", binary_count=1), project("c", binary_count=8)]
    result = allocation(projects, 1000, make_policy(cap_pct=Decimal("60"), minimum=100))
    amounts = {line.upstream_key: line.amount_cents for line in result.lines}
    assert amounts == {"a": 200, "b": 200, "c": 600}


def test_bounded_sweep_activates_minimum_bound():
    projects = [project("a", binary_count=1), project("b", binary_count=1), project("c", binary_count=8)]
    result = allocation(projects, 1000, make_policy(cap_pct=Decimal("60"), minimum=300))
    amounts = {line.upstream_key: line.amount_cents for line in result.lines}
    assert amounts == {"a": 300, "b": 300, "c": 400}


def test_equal_breakpoints_are_grouped_including_equal_lower_upper():
    # m == c is feasible only for a one-project budget at that amount.
    result = allocation([project("only", binary_count=2)], 100, make_policy(cap_pct=Decimal("100"), minimum=100))
    assert [(line.upstream_key, line.amount_cents) for line in result.lines] == [("only", 100)]


def test_repeating_shares_exact_remainders_tie_by_upstream_key():
    projects = [project("c"), project("a"), project("b")]
    result = allocation(projects, 1001, make_policy(minimum=100))
    assert {line.upstream_key: line.amount_cents for line in result.lines} == {"a": 334, "b": 334, "c": 333}


def test_equal_nonterminating_rational_remainders_terminate_and_tie_by_key():
    policy = AllocationPolicy(
        AllocationRules(304, Decimal("100"), 1),
        NeedFactorPolicy(*(Decimal("1") for _ in range(5))),
        FeePolicy(False, None, None, None, None, 0),
    )
    projects = [
        project("z", binary_count=301),
        project("b", binary_count=304),
        project("a", binary_count=301),
    ]

    first = allocation(projects, 302, policy)
    second = allocation(list(reversed(projects)), 302, policy)
    expected = [("a", 101), ("b", 101), ("z", 100)]

    assert [(line.upstream_key, line.amount_cents) for line in first.lines] == expected
    assert [(line.upstream_key, line.amount_cents) for line in second.lines] == expected


def test_extremely_close_remainders_use_exact_order_not_decimal_approximation():
    denominator = 2 * 10**30
    midpoint_numerator = 1001 * 10**30
    shares = [(midpoint_numerator - 1, denominator), (midpoint_numerator + 1, denominator)]
    # Shares are 500.5 +/- 5e-31 cents; the second exact remainder wins.
    assert _round_shares(shares, ["a", "b"], 1001, 100, 900) == [500, 501]


def test_optimized_remainder_sort_matches_independent_exact_selection_oracle():
    remainders = [(1, 3), (2, 5), (2, 6), (999, 2000), (1, 2)]
    keys = ["z", "a", "b", "c", "d"]
    remaining = set(range(len(remainders)))
    reference = []
    while remaining:
        best = None
        for candidate in remaining:
            if best is None:
                best = candidate
                continue
            candidate_scaled = remainders[candidate][0] * remainders[best][1]
            best_scaled = remainders[best][0] * remainders[candidate][1]
            if candidate_scaled > best_scaled or (
                candidate_scaled == best_scaled and keys[candidate] < keys[best]
            ):
                best = candidate
        reference.append(best)
        remaining.remove(best)
    assert _rank_exact_remainders(remainders, keys) == reference == [4, 3, 1, 2, 0]


def test_engine_orders_close_but_unequal_remainders_exactly():
    policy = make_policy(factors={"inactive_12m": Decimal("1.0000000000000000000000000000000000000001")})
    projects = [
        project("a", last_commit=date(2025, 11, 6)),
        project("b", last_commit=date(2026, 1, 1)),
    ]
    result = allocation(projects, 1001, policy)
    assert {line.upstream_key: line.amount_cents for line in result.lines} == {"a": 501, "b": 500}


def test_zero_budget_returns_empty_result_and_zero_totals():
    result = allocation([project("a")], 0)
    assert result.lines == []
    assert (result.payable_total_cents, result.reserved_total_cents) == (0, 0)
    assert result.est_payout_fees_cents is None


def test_payable_reserved_split_and_fee_estimate_only_when_measured():
    projects = [project("pay", opted_in=True), project("reserve", opted_in=False)]
    result = allocation(projects, 200, make_policy(minimum=100, measured=True, payout_fee=7))
    assert sum(line.payable_cents for line in result.lines) == result.payable_total_cents == 100
    assert sum(line.reserved_cents for line in result.lines) == result.reserved_total_cents == 100
    assert result.est_payout_fees_cents == 7


def test_unmeasured_fee_estimate_stays_unavailable_and_zero_payable_is_valid():
    result = allocation([project("reserved", opted_in=False)], 100)
    assert result.payable_total_cents == 0
    assert result.reserved_total_cents == 100
    assert result.est_payout_fees_cents is None


def test_zero_payable_measured_fee_is_zero():
    result = allocation([project("reserved", opted_in=False)], 100, make_policy(measured=True, payout_fee=7))
    assert result.est_payout_fees_cents == 0


def test_determinism_under_reordered_inputs_and_global_context_unchanged():
    projects = [project("z", binary_count=2, committers=2), project("a", binary_count=2, committers=2)]
    original = getcontext().copy()
    try:
        getcontext().prec = 7
        before = getcontext().copy()
        one = allocation(projects, 1001, make_policy(minimum=100))
        two = allocation(list(reversed(projects)), 1001, make_policy(minimum=100))
        assert one == two
        after = getcontext()
        assert (after.prec, after.rounding, after.Emin, after.Emax, after.clamp) == (
            before.prec, before.rounding, before.Emin, before.Emax, before.clamp
        )
        assert after.flags == before.flags
        assert after.traps == before.traps
    finally:
        setcontext(original)


@pytest.mark.parametrize("budget", [1, 99, 101, 201, 301, 999])
def test_infeasible_or_feasible_budget_never_returns_partial_result(budget):
    try:
        result = allocation([project("a"), project("b")], budget, make_policy(cap_pct=Decimal("60"), minimum=100))
    except AllocationError:
        return
    assert sum(line.amount_cents for line in result.lines) == budget
    assert all(100 <= line.amount_cents <= 600 for line in result.lines)


def test_exhaustive_small_equal_weight_feasibility_matches_active_count_condition():
    projects = [project(str(index)) for index in range(4)]
    for budget in range(1, 31):
        for minimum in range(1, 7):
            cap = budget // 2
            policy = make_policy(cap_pct=Decimal("50"), minimum=minimum)
            feasible = cap >= minimum and max(1, (budget + cap - 1) // cap) <= min(4, budget // minimum)
            try:
                result = allocation(projects, budget, policy)
            except AllocationError:
                assert not feasible, (budget, minimum, cap)
            else:
                assert feasible, (budget, minimum, cap)
                assert sum(line.amount_cents for line in result.lines) == budget
                assert all(minimum <= line.amount_cents <= cap for line in result.lines)


def test_internal_rounding_invariant_failure_rejects_result():
    with pytest.raises(AllocationInvariantError):
        _round_shares([(100, 1)], ["a"], 101, 100, 100)
