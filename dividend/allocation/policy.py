"""Strict allocation policy models and YAML loader."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from ..config import DEFAULT_POLICY_PATH


MAX_DECIMAL_SIGNIFICANT_DIGITS = 1_000
MIN_DECIMAL_EXPONENT = -10_000
MAX_DECIMAL_EXPONENT = 10_000


class AllocationPolicyError(ValueError):
    """Invalid or unreadable allocation policy configuration."""


def _strict_int(field: str, value: object, *, minimum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if value < minimum:
        if minimum == 0:
            raise ValueError(f"{field} must be nonnegative")
        raise ValueError(f"{field} must be positive")


def _strict_decimal(field: str, value: object) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{field} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return _canonical_bounded_decimal(field, value)


def _canonical_bounded_decimal(field: str, value: Decimal) -> Decimal:
    """Validate and canonicalize Decimal tuples without Decimal.normalize().

    Trailing coefficient zeros are removed by tuple manipulation, so
    equivalent representations receive identical significant-digit and
    exponent checks independent of the active Decimal context.
    """
    parts = value.as_tuple()
    digits = parts.digits
    first = 0
    while first < len(digits) and digits[first] == 0:
        first += 1
    if first == len(digits):
        return Decimal(0)
    last = len(digits)
    while digits[last - 1] == 0:
        last -= 1
    significant = digits[first:last]
    exponent = parts.exponent + (len(digits) - last)
    if len(significant) > MAX_DECIMAL_SIGNIFICANT_DIGITS:
        raise ValueError(
            f"{field} exceeds {MAX_DECIMAL_SIGNIFICANT_DIGITS} significant digits"
        )
    if not MIN_DECIMAL_EXPONENT <= exponent <= MAX_DECIMAL_EXPONENT:
        raise ValueError(
            f"{field} exponent must be between {MIN_DECIMAL_EXPONENT} and "
            f"{MAX_DECIMAL_EXPONENT} after canonicalization"
        )
    return Decimal((parts.sign, significant, exponent))


def _policy_decimal(field: str, value: object, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    # YAML integer scalars are exact and can represent decimal-valued fields
    # such as the documented 15 percent cap without passing through float.
    if type(value) is int:
        value = Decimal(value)
    if type(value) is not Decimal:
        expected = "a Decimal or null" if nullable else "a Decimal"
        raise AllocationPolicyError(f"{field} must be {expected}")
    try:
        return _strict_decimal(field, value)
    except (TypeError, ValueError) as exc:
        raise AllocationPolicyError(f"{field}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class AllocationRules:
    max_binaries_cap: int
    per_project_cap_pct: Decimal
    min_line_cents: int

    def __post_init__(self) -> None:
        _strict_int("max_binaries_cap", self.max_binaries_cap, minimum=1)
        cap = _strict_decimal("per_project_cap_pct", self.per_project_cap_pct)
        object.__setattr__(self, "per_project_cap_pct", cap)
        if not Decimal(0) < cap <= Decimal(100):
            raise ValueError("per_project_cap_pct must be in (0, 100]")
        _strict_int("min_line_cents", self.min_line_cents, minimum=1)


@dataclass(frozen=True, slots=True)
class NeedFactorPolicy:
    archived: Decimal
    inactive_12m: Decimal
    committers_1: Decimal
    committers_2_5: Decimal
    committers_6_plus: Decimal

    def __post_init__(self) -> None:
        for field in (
            "archived", "inactive_12m", "committers_1", "committers_2_5",
            "committers_6_plus",
        ):
            factor = _strict_decimal(field, getattr(self, field))
            object.__setattr__(self, field, factor)
            if factor <= 0:
                raise ValueError(f"{field} must be positive")


@dataclass(frozen=True, slots=True)
class FeePolicy:
    measured: bool
    order_fee_pct: Decimal | None
    order_fee_fixed_cents: int | None
    payout_fee_per_item_cents: int | None
    buffer_cents: int | None
    tolerance_cents: int
    version: str = "unversioned"
    merchant_market: str | None = None
    payer_market_assumption: str | None = None
    recipient_market: str | None = None
    currency: str | None = None
    checkout_method: str | None = None
    payout_method: str | None = None
    pricing_tier: str | None = None
    currency_conversion: bool = False
    micropayments: bool = False
    charity_pricing: bool = False
    negotiated_rates: bool = False

    def __post_init__(self) -> None:
        if type(self.measured) is not bool:
            raise TypeError("measured must be a bool")
        if self.order_fee_pct is not None:
            percentage = _strict_decimal("order_fee_pct", self.order_fee_pct)
            object.__setattr__(self, "order_fee_pct", percentage)
            if not Decimal(0) <= percentage < Decimal(100):
                raise ValueError("order_fee_pct must be in [0, 100)")
        for field in (
            "order_fee_fixed_cents", "payout_fee_per_item_cents", "buffer_cents",
        ):
            amount = getattr(self, field)
            if amount is not None:
                _strict_int(field, amount, minimum=0)
        _strict_int("tolerance_cents", self.tolerance_cents, minimum=0)
        for field in ("version", "merchant_market", "payer_market_assumption", "recipient_market",
                      "currency", "checkout_method", "payout_method", "pricing_tier"):
            value = getattr(self, field)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{field} must be a nonempty string or null")
        for field in ("currency_conversion", "micropayments", "charity_pricing", "negotiated_rates"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a bool")
        if self.measured:
            required = (
                "order_fee_pct", "order_fee_fixed_cents",
                "payout_fee_per_item_cents", "buffer_cents",
            )
            missing = [field for field in required if getattr(self, field) is None]
            if missing:
                raise ValueError(
                    "measured fee policy requires non-null: " + ", ".join(missing)
                )

    @property
    def supports_standard_us_usd(self) -> bool:
        return (
            self.version == "paypal-us-standard-v1"
            and self.merchant_market == self.payer_market_assumption == self.recipient_market == "US"
            and self.currency == "USD"
            and self.checkout_method == "standard_paypal_checkout"
            and self.payout_method == "paypal_payouts_api"
            and self.pricing_tier == "standard"
            and not any((self.currency_conversion, self.micropayments,
                         self.charity_pricing, self.negotiated_rates))
            and self.order_fee_pct == Decimal("3.49")
            and self.order_fee_fixed_cents == 49
            and self.payout_fee_per_item_cents == 25
        )


@dataclass(frozen=True, slots=True)
class AllocationPolicy:
    allocation: AllocationRules
    need_factors: NeedFactorPolicy
    fees: FeePolicy

    def __post_init__(self) -> None:
        for field, expected in (
            ("allocation", AllocationRules),
            ("need_factors", NeedFactorPolicy),
            ("fees", FeePolicy),
        ):
            if not isinstance(getattr(self, field), expected):
                raise TypeError(f"{field} must be {expected.__name__}")


class _PolicyLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves decimal scalars without binary floats."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "expected a mapping", node.start_mark)
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found an unhashable key", key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"duplicate key {key!r}", key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _construct_decimal(loader: _PolicyLoader, node) -> Decimal:
    scalar = loader.construct_scalar(node).replace("_", "")
    normalized = scalar.lower()
    if normalized in {".nan", "+.nan", "-.nan"}:
        scalar = "NaN"
    elif normalized in {".inf", "+.inf"}:
        scalar = "Infinity"
    elif normalized == "-.inf":
        scalar = "-Infinity"
    try:
        return Decimal(scalar)
    except InvalidOperation as exc:
        raise ConstructorError(
            "while constructing a Decimal", node.start_mark,
            f"invalid Decimal scalar {scalar!r}", node.start_mark,
        ) from exc


_PolicyLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def _require_mapping(field: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AllocationPolicyError(f"{field} must be a mapping")
    for key in value:
        if type(key) is not str:
            raise AllocationPolicyError(f"{field} keys must be strings")
    return value


def _check_keys(field: str, value: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required)
    if missing:
        raise AllocationPolicyError(f"{field} missing required key(s): {', '.join(missing)}")
    if unknown:
        raise AllocationPolicyError(f"{field} has unknown key(s): {', '.join(unknown)}")


def _read_int(field: str, value: object, *, minimum: int) -> int:
    try:
        _strict_int(field, value, minimum=minimum)
    except (TypeError, ValueError) as exc:
        raise AllocationPolicyError(f"{field}: {exc}") from exc
    return value  # type: ignore[return-value]


def _build_policy(raw: object) -> AllocationPolicy:
    root = _require_mapping("policy", raw)
    _check_keys("policy", root, {"measured", "fees", "allocation", "need_factor"})

    fee_values = _require_mapping("fees", root["fees"])
    _check_keys("fees", fee_values, {
        "order_fee_pct", "order_fee_fixed_cents", "payout_fee_per_item_cents",
        "buffer_cents", "tolerance_cents", "version", "merchant_market",
        "payer_market_assumption", "recipient_market", "currency", "checkout_method",
        "payout_method", "pricing_tier", "currency_conversion", "micropayments",
        "charity_pricing", "negotiated_rates",
    })
    allocation_values = _require_mapping("allocation", root["allocation"])
    _check_keys("allocation", allocation_values, {
        "max_binaries_cap", "per_project_cap_pct", "min_line_cents",
    })
    factor_values = _require_mapping("need_factor", root["need_factor"])
    _check_keys("need_factor", factor_values, {
        "archived", "inactive_12m", "committers_1", "committers_2_5",
        "committers_6_plus",
    })

    measured = root["measured"]
    if type(measured) is not bool:
        raise AllocationPolicyError("measured must be a bool")

    try:
        fees = FeePolicy(
            measured=measured,
            order_fee_pct=_policy_decimal("fees.order_fee_pct", fee_values["order_fee_pct"], nullable=True),
            order_fee_fixed_cents=(
                None if fee_values["order_fee_fixed_cents"] is None else
                _read_int("fees.order_fee_fixed_cents", fee_values["order_fee_fixed_cents"], minimum=0)
            ),
            payout_fee_per_item_cents=(
                None if fee_values["payout_fee_per_item_cents"] is None else
                _read_int("fees.payout_fee_per_item_cents", fee_values["payout_fee_per_item_cents"], minimum=0)
            ),
            buffer_cents=(
                None if fee_values["buffer_cents"] is None else
                _read_int("fees.buffer_cents", fee_values["buffer_cents"], minimum=0)
            ),
            tolerance_cents=_read_int("fees.tolerance_cents", fee_values["tolerance_cents"], minimum=0),
            version=fee_values["version"],
            merchant_market=fee_values["merchant_market"],
            payer_market_assumption=fee_values["payer_market_assumption"],
            recipient_market=fee_values["recipient_market"],
            currency=fee_values["currency"],
            checkout_method=fee_values["checkout_method"],
            payout_method=fee_values["payout_method"],
            pricing_tier=fee_values["pricing_tier"],
            currency_conversion=fee_values["currency_conversion"],
            micropayments=fee_values["micropayments"],
            charity_pricing=fee_values["charity_pricing"],
            negotiated_rates=fee_values["negotiated_rates"],
        )
        allocation = AllocationRules(
            max_binaries_cap=_read_int("allocation.max_binaries_cap", allocation_values["max_binaries_cap"], minimum=1),
            per_project_cap_pct=_policy_decimal("allocation.per_project_cap_pct", allocation_values["per_project_cap_pct"]),
            min_line_cents=_read_int("allocation.min_line_cents", allocation_values["min_line_cents"], minimum=1),
        )
        try:
            parsed_factors = {}
            for key, value in factor_values.items():
                factor = _policy_decimal(f"need_factor.{key}", value)
                if factor <= 0:
                    raise AllocationPolicyError(f"need_factor.{key} must be positive")
                parsed_factors[key] = factor
            factors = NeedFactorPolicy(**parsed_factors)
        except (TypeError, ValueError) as exc:
            raise AllocationPolicyError(f"need_factor: {exc}") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, AllocationPolicyError):
            raise
        raise AllocationPolicyError(str(exc)) from exc
    return AllocationPolicy(allocation=allocation, need_factors=factors, fees=fees)


def load_allocation_policy(
    path: str | Path = DEFAULT_POLICY_PATH,
) -> AllocationPolicy:
    """Load and strictly validate the configured allocation policy YAML."""
    policy_path = Path(path)
    try:
        with policy_path.open(encoding="utf-8") as policy_file:
            raw = yaml.load(policy_file, Loader=_PolicyLoader)
    except OSError as exc:
        raise AllocationPolicyError(f"policy: could not read {policy_path}") from exc
    except yaml.YAMLError as exc:
        raise AllocationPolicyError(f"policy: invalid YAML: {exc}") from exc
    return _build_policy(raw)
