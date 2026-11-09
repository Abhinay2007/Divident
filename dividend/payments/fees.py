"""Integer-cent fee estimates for the explicitly supported PayPal scope."""

INCOMING_RATE_BASIS_POINTS = 349
INCOMING_FIXED_CENTS = 49
PAYOUT_FEE_PER_ITEM_CENTS = 25
ROUNDING_POLICY = "half_up_to_cent"


def estimate_incoming_fee_cents(gross_cents: int) -> int:
    if type(gross_cents) is not int or gross_cents < 0:
        raise ValueError("gross_cents must be a nonnegative integer")
    percentage = (gross_cents * INCOMING_RATE_BASIS_POINTS + 5_000) // 10_000
    return percentage + INCOMING_FIXED_CENTS


def estimate_payout_fee_cents(item_count: int) -> int:
    if type(item_count) is not int or item_count < 0:
        raise ValueError("item_count must be a nonnegative integer")
    return item_count * PAYOUT_FEE_PER_ITEM_CENTS


def calculate_gross_charge_cents(target_distribution_cents: int, payout_fee_cents: int,
                                 buffer_cents: int = 0) -> tuple[int, int]:
    if any(type(value) is not int or value < 0
           for value in (target_distribution_cents, payout_fee_cents, buffer_cents)):
        raise ValueError("charge inputs must be nonnegative integer cents")
    target_net = target_distribution_cents + payout_fee_cents + buffer_cents
    # Invert the percentage-plus-fixed formula with exact integer arithmetic,
    # then verify the rounded fee and increase by a cent only if needed.
    gross = ((target_net + INCOMING_FIXED_CENTS) * 10_000 +
             (10_000 - INCOMING_RATE_BASIS_POINTS) - 1) // (10_000 - INCOMING_RATE_BASIS_POINTS)
    while gross - estimate_incoming_fee_cents(gross) < target_net:
        gross += 1
    while gross > 0 and gross - 1 - estimate_incoming_fee_cents(gross - 1) >= target_net:
        gross -= 1
    return gross, estimate_incoming_fee_cents(gross)
