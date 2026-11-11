const money = (cents) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD',
}).format(cents / 100)

export function captureConfirmationMessage(amountCents) {
  return `Capture the approved PayPal order for ${money(amountCents)} USD?`
}

export function approvalUnavailableReason(plan) {
  if (plan.payable_total_cents <= 0) {
    return 'This plan has no payable allocations, so there is no amount to fund.'
  }
  if (plan.charge_cents === null || plan.est_payout_fees_cents === null) {
    return 'Required PayPal fee estimates are unavailable under the configured policy.'
  }
  return null
}
