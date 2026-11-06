"""Pure deterministic bounded allocation engine."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import (
    Context, Decimal, DecimalException, Inexact, MAX_EMAX, MIN_EMIN,
    ROUND_HALF_EVEN, Rounded, localcontext,
)
from functools import cmp_to_key
from math import gcd

from .policy import AllocationPolicy
from .policy import MAX_DECIMAL_EXPONENT, MAX_DECIMAL_SIGNIFICANT_DIGITS
from .types import FactsSnapshot, ProjectInput


class AllocationError(ValueError):
    """Invalid allocation input or infeasible budget (no public code assigned)."""


class AllocationInvariantError(RuntimeError):
    """The engine's internal or final allocation invariants were violated."""


@dataclass(frozen=True, slots=True)
class AllocationLine:
    upstream_key: str
    display_name: str
    weight: Decimal
    amount_cents: int
    payable_cents: int
    reserved_cents: int
    reason: str
    facts_snapshot: FactsSnapshot


@dataclass(slots=True)
class AllocationResult:
    lines: list[AllocationLine]
    payable_total_cents: int
    reserved_total_cents: int
    est_payout_fees_cents: int | None


# A positive rational is represented by a reduced (numerator, denominator)
# pair. Decimal is kept as the domain and intermediate representation; these
# integer pairs certify comparisons and cent decisions without Fraction/float.
_Ratio = tuple[int, int]
_MAX_APPLICABLE_NEED_FACTORS = 3
_MAX_DERIVED_FACTOR_DIGITS = _MAX_APPLICABLE_NEED_FACTORS * MAX_DECIMAL_SIGNIFICANT_DIGITS
_MAX_DERIVED_FACTOR_EXPONENT = _MAX_APPLICABLE_NEED_FACTORS * MAX_DECIMAL_EXPONENT


def _private_context(precision: int):
    return localcontext(Context(
        prec=max(precision, 28), rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN, Emax=MAX_EMAX, capitals=1, clamp=0,
    ))


def _ratio(numerator: int, denominator: int = 1) -> _Ratio:
    if denominator == 0:
        raise AllocationInvariantError("zero rational denominator")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    divisor = gcd(abs(numerator), denominator)
    return numerator // divisor, denominator // divisor


def _decimal_ratio(value: Decimal) -> _Ratio:
    if not value.is_finite():
        raise AllocationError("weights and policy decimals must be finite")
    parts = value.as_tuple()
    if abs(parts.exponent) > _MAX_DERIVED_FACTOR_EXPONENT:
        raise AllocationError("derived Decimal exponent exceeds its verified envelope")
    coefficient = 0
    for start in range(0, len(parts.digits), 9):
        chunk = parts.digits[start:start + 9]
        chunk_value = 0
        for digit in chunk:
            chunk_value = chunk_value * 10 + digit
        coefficient = coefficient * (10 ** len(chunk)) + chunk_value
    if parts.sign:
        coefficient = -coefficient
    exponent = parts.exponent
    if exponent >= 0:
        return _ratio(coefficient * (10 ** exponent))
    return _ratio(coefficient, 10 ** (-exponent))


def _cmp_ratio(left: _Ratio, right: _Ratio) -> int:
    lhs = left[0] * right[1]
    rhs = right[0] * left[1]
    return (lhs > rhs) - (lhs < rhs)


def _add(left: _Ratio, right: _Ratio) -> _Ratio:
    return _ratio(left[0] * right[1] + right[0] * left[1], left[1] * right[1])


def _sub(left: _Ratio, right: _Ratio) -> _Ratio:
    return _ratio(left[0] * right[1] - right[0] * left[1], left[1] * right[1])


def _mul(left: _Ratio, right: _Ratio) -> _Ratio:
    return _ratio(left[0] * right[0], left[1] * right[1])


def _div(left: _Ratio, right: _Ratio) -> _Ratio:
    if right[0] == 0:
        raise AllocationInvariantError("division by zero in exact allocation")
    return _ratio(left[0] * right[1], left[1] * right[0])


def _digits(value: Decimal) -> int:
    return len(value.as_tuple().digits)


def _int_text(value: int) -> str:
    """Format arbitrary-size integers without Python's int-to-str digit cap."""
    return format(Decimal(value), "f")


def _year_cutoff(evaluation_date: date) -> date:
    target_year = evaluation_date.year - 1
    target_day = min(evaluation_date.day, calendar.monthrange(target_year, evaluation_date.month)[1])
    return date(target_year, evaluation_date.month, target_day)


def _derive_weight(
    project: ProjectInput, policy: AllocationPolicy, evaluation_date: date,
) -> tuple[Decimal, Decimal, int, tuple[tuple[str, Decimal], ...]]:
    facts = project.facts
    factors = [Decimal(1)]
    applicable: list[tuple[str, Decimal]] = []
    if facts.archived is True:
        applicable.append(("archived", policy.need_factors.archived))
    if facts.last_commit is not None:
        if facts.last_commit > evaluation_date:
            raise AllocationError(f"future last_commit for {project.upstream_key}")
        if facts.last_commit <= _year_cutoff(evaluation_date):
            applicable.append(("inactive_12m", policy.need_factors.inactive_12m))
    count = facts.committers_12m
    if count == 1:
        applicable.append(("committers_1", policy.need_factors.committers_1))
    elif count is not None and 2 <= count <= 5:
        applicable.append(("committers_2_5", policy.need_factors.committers_2_5))
    elif count is not None and count >= 6:
        applicable.append(("committers_6_plus", policy.need_factors.committers_6_plus))

    usage = min(project.binary_count, policy.allocation.max_binaries_cap)
    try:
        usage_decimal = Decimal(usage)
        usage_digits = len(usage_decimal.as_tuple().digits)
        factor_digits = sum(_digits(factor) for _, factor in applicable)
        precision = max(28, factor_digits + usage_digits + 1)
        with _private_context(precision) as context:
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            need_factor = Decimal(1)
            for _, factor in applicable:
                need_factor *= factor
            weight = usage_decimal * need_factor
    except (DecimalException, ValueError) as exc:
        raise AllocationError("Decimal weight derivation failed") from exc
    if not weight.is_finite() or weight <= 0:
        raise AllocationError(f"weight must be finite and positive for {project.upstream_key}")
    need_parts = need_factor.as_tuple()
    weight_parts = weight.as_tuple()
    if (
        len(need_parts.digits) > _MAX_DERIVED_FACTOR_DIGITS
        or abs(need_parts.exponent) > _MAX_DERIVED_FACTOR_EXPONENT
        or len(weight_parts.digits) > _MAX_DERIVED_FACTOR_DIGITS + usage_digits
        or abs(weight_parts.exponent) > _MAX_DERIVED_FACTOR_EXPONENT
    ):
        raise AllocationError("derived weight exceeds its verified resource envelope")
    return weight, need_factor, usage, tuple(applicable)


def _cap_cents(budget_cents: int, percentage: Decimal) -> int:
    budget_ratio = (budget_cents, 1)
    percentage_ratio = _decimal_ratio(percentage)
    exact = _div(_mul(budget_ratio, percentage_ratio), (100, 1))
    return exact[0] // exact[1]


def _share_scale(
    weights: list[Decimal], minimum: int, cap: int, budget: int,
) -> _Ratio:
    """Finite exact breakpoint sweep; Decimal shares are checked separately."""
    weight_ratios = [_decimal_ratio(weight) for weight in weights]
    lower_state = set(range(len(weights)))
    free_state: set[int] = set()
    capped_state: set[int] = set()
    events: list[tuple[_Ratio, str, int]] = []
    for index, weight in enumerate(weight_ratios):
        events.append((_div((minimum, 1), weight), "lower", index))
        events.append((_div((cap, 1), weight), "upper", index))
    events.sort(key=cmp_to_key(lambda a, b: _cmp_ratio(a[0], b[0])))

    constant = minimum * len(weights)
    previous: _Ratio = (0, 1)
    free_weight_sum: _Ratio = (0, 1)
    cursor = 0
    while cursor < len(events):
        threshold = events[cursor][0]
        at_previous = _add((constant, 1), _mul(previous, free_weight_sum))
        at_threshold = _add((constant, 1), _mul(threshold, free_weight_sum))
        target = (budget, 1)
        if _cmp_ratio(target, at_previous) == 0:
            return previous
        if _cmp_ratio(target, at_threshold) == 0:
            return threshold
        if _cmp_ratio(target, at_previous) > 0 and _cmp_ratio(target, at_threshold) < 0:
            if free_weight_sum[0] <= 0:
                raise AllocationInvariantError("positive budget interval has no free weights")
            return _div(_sub(target, (constant, 1)), free_weight_sum)

        end = cursor + 1
        while end < len(events) and _cmp_ratio(events[end][0], threshold) == 0:
            end += 1
        group = events[cursor:end]
        # Apply all lower transitions before upper transitions at an equal
        # threshold. This also handles m == c without an intermediate state.
        for _, kind, index in group:
            if kind == "lower":
                if index not in lower_state:
                    raise AllocationInvariantError("duplicate lower breakpoint transition")
                lower_state.remove(index)
                free_state.add(index)
                free_weight_sum = _add(free_weight_sum, weight_ratios[index])
        for _, kind, index in group:
            if kind == "upper":
                if index not in free_state:
                    raise AllocationInvariantError("upper breakpoint without free project")
                free_state.remove(index)
                capped_state.add(index)
                free_weight_sum = _sub(free_weight_sum, weight_ratios[index])
        constant = minimum * len(lower_state) + cap * len(capped_state)
        previous = threshold
        cursor = end

    if budget == cap * len(weights):
        return events[-1][0]
    raise AllocationInvariantError("feasible budget was not located by breakpoint sweep")


def _bounded_exact_shares(
    weights: list[Decimal], scale: _Ratio, minimum: int, cap: int,
) -> list[_Ratio]:
    result: list[_Ratio] = []
    for weight in weights:
        share = _mul(scale, _decimal_ratio(weight))
        if _cmp_ratio(share, (minimum, 1)) < 0:
            share = (minimum, 1)
        elif _cmp_ratio(share, (cap, 1)) > 0:
            share = (cap, 1)
        result.append(share)
    return result


def _rank_exact_remainders(remainders: list[_Ratio], keys: list[str]) -> list[int]:
    return sorted(
        range(len(remainders)),
        key=cmp_to_key(lambda a, b: (
            -_cmp_ratio(remainders[a], remainders[b])
            or ((keys[a] > keys[b]) - (keys[a] < keys[b]))
        )),
    )


def _round_shares(shares: list[_Ratio], keys: list[str], budget: int, minimum: int, cap: int) -> list[int]:
    floors: list[int] = []
    remainders: list[_Ratio] = []
    for numerator, denominator in shares:
        floor_value, remainder = divmod(numerator, denominator)
        floors.append(floor_value)
        remainders.append(_ratio(remainder, denominator))
    leftover = budget - sum(floors)
    if not 0 <= leftover <= len(shares):
        raise AllocationInvariantError("largest-remainder cent count is outside its valid range")
    order = _rank_exact_remainders(remainders, keys)
    positive_remainders = [index for index in order if remainders[index][0] > 0]
    if leftover > len(positive_remainders):
        raise AllocationInvariantError("leftover cents exceed positive fractional shares")
    for index in positive_remainders[:leftover]:
        floors[index] += 1
    if any(amount < minimum or amount > cap for amount in floors):
        raise AllocationInvariantError("rounded allocation violates minimum or cap")
    if sum(floors) != budget:
        raise AllocationInvariantError("rounded allocations do not conserve budget")
    return floors


def _validate_final(lines: list[AllocationLine], budget: int, payable_total: int, reserved_total: int) -> None:
    if len({line.upstream_key for line in lines}) != len(lines):
        raise AllocationInvariantError("duplicate allocation output key")
    if lines != sorted(lines, key=lambda line: line.upstream_key):
        raise AllocationInvariantError("allocation output is not canonically ordered")
    if sum(line.amount_cents for line in lines) != budget:
        raise AllocationInvariantError("allocation lines do not equal budget")
    if any(line.payable_cents + line.reserved_cents != line.amount_cents for line in lines):
        raise AllocationInvariantError("payable and reserved amounts do not equal line amount")
    if sum(line.payable_cents for line in lines) != payable_total:
        raise AllocationInvariantError("payable total does not match lines")
    if sum(line.reserved_cents for line in lines) != reserved_total:
        raise AllocationInvariantError("reserved total does not match lines")


def allocate(
    projects: list[ProjectInput],
    budget_cents: int,
    policy: AllocationPolicy,
    evaluation_date: date,
) -> AllocationResult:
    """Allocate a budget using deterministic bounded weighted shares."""
    if type(projects) is not list:
        raise AllocationError("projects must be a list")
    if not projects:
        raise AllocationError("engine requires at least one eligible project")
    if type(budget_cents) is not int or budget_cents < 0:
        raise AllocationError("budget_cents must be a nonnegative integer")
    if type(policy) is not AllocationPolicy:
        raise AllocationError("policy must be AllocationPolicy")
    if type(evaluation_date) is not date:
        raise AllocationError("evaluation_date must be a date")
    if not all(type(project) is ProjectInput for project in projects):
        raise AllocationError("projects must contain ProjectInput records")
    keys = [project.upstream_key for project in projects]
    if len(keys) != len(set(keys)):
        raise AllocationError("duplicate upstream_key")

    calculated: list[tuple[ProjectInput, Decimal, Decimal, int, tuple[tuple[str, Decimal], ...]]] = []
    for project in projects:
        weight, need_factor, usage, applicable = _derive_weight(project, policy, evaluation_date)
        calculated.append((project, weight, need_factor, usage, applicable))

    if budget_cents == 0:
        return AllocationResult([], 0, 0, 0 if policy.fees.measured and policy.fees.payout_fee_per_item_cents is not None else None)

    minimum = policy.allocation.min_line_cents
    cap = _cap_cents(budget_cents, policy.allocation.per_project_cap_pct)
    if cap < minimum:
        raise AllocationError("per-project cap is below minimum line amount")
    project_count = len(calculated)
    lower_count = max(1, (budget_cents + cap - 1) // cap)
    upper_count = min(project_count, budget_cents // minimum)
    if lower_count > upper_count:
        raise AllocationError("budget is infeasible under project minimum and cap")
    active_count = upper_count
    ranked = sorted(calculated, key=lambda item: (-item[1], item[0].upstream_key))
    active = ranked[:active_count]
    active.sort(key=lambda item: item[0].upstream_key)
    active_projects = [item[0] for item in active]
    active_weights = [item[1] for item in active]

    scale = _share_scale(active_weights, minimum, cap, budget_cents)
    exact_shares = _bounded_exact_shares(active_weights, scale, minimum, cap)
    if sum((share[0] for share in exact_shares), 0) == 0:
        raise AllocationInvariantError("exact shares are empty")
    share_sum: _Ratio = (0, 1)
    for share in exact_shares:
        share_sum = _add(share_sum, share)
    if _cmp_ratio(share_sum, (budget_cents, 1)) != 0:
        raise AllocationInvariantError("bounded exact shares do not sum to budget")

    # The breakpoint sweep and shares are exact reduced rationals derived from
    # the Decimal weights. They alone decide bounds, floors, remainders, and
    # ties, so non-terminating Decimal representations cannot affect results.
    active_keys = [project.upstream_key for project in active_projects]
    amounts = _round_shares(exact_shares, [project.upstream_key for project in active_projects], budget_cents, minimum, cap)
    lines: list[AllocationLine] = []
    for (project, weight, need_factor, usage, applicable), amount in zip(active, amounts, strict=True):
        payable = amount if project.opted_in else 0
        reserved = amount - payable
        usage_reason = (
            f"Capped binary usage: min({_int_text(project.binary_count)}, "
            f"{_int_text(policy.allocation.max_binaries_cap)}) = {_int_text(usage)}."
        )
        factors_reason = "; ".join(
            f"{name} factor {factor}" for name, factor in applicable
        ) or "no applicable need factors (neutral 1.0)"
        reason = (
            f"{usage_reason} Need factors: {factors_reason}. "
            f"Combined need factor: {need_factor}."
        )
        lines.append(AllocationLine(
            upstream_key=project.upstream_key,
            display_name=project.display_name,
            weight=weight,
            amount_cents=amount,
            payable_cents=payable,
            reserved_cents=reserved,
            reason=reason,
            facts_snapshot=project.facts.snapshot,
        ))
    payable_total = sum(line.payable_cents for line in lines)
    reserved_total = sum(line.reserved_cents for line in lines)
    _validate_final(lines, budget_cents, payable_total, reserved_total)
    fee_per_item = policy.fees.payout_fee_per_item_cents
    est_fees: int | None = None
    if policy.fees.measured and fee_per_item is not None:
        est_fees = sum(1 for line in lines if line.payable_cents > 0) * fee_per_item
    return AllocationResult(lines, payable_total, reserved_total, est_fees)
