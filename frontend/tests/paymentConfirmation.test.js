import test from 'node:test'
import assert from 'node:assert/strict'
import { approvalUnavailableReason, captureConfirmationMessage } from '../src/paymentConfirmation.js'

test('capture confirmation names the exact USD charge', () => {
  assert.equal(captureConfirmationMessage(840),
    'Capture the approved PayPal order for $8.40 USD?')
})

test('zero payable allocations are not reported as unmeasured fees', () => {
  assert.equal(approvalUnavailableReason({ payable_total_cents: 0, charge_cents: null,
    est_payout_fees_cents: 0 }),
  'This plan has no payable allocations, so there is no amount to fund.')
})

test('missing fee estimates still explain why approval is unavailable', () => {
  assert.equal(approvalUnavailableReason({ payable_total_cents: 100, charge_cents: null,
    est_payout_fees_cents: null }),
  'Required PayPal fee estimates are unavailable under the configured policy.')
})
