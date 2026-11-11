export function captureConfirmationMessage(amountCents: number | null): string
export function approvalUnavailableReason(plan: {
  payable_total_cents: number
  charge_cents: number | null
  est_payout_fees_cents: number | null
}): string | null
