import test from 'node:test'
import assert from 'node:assert/strict'
import { payoutConfirmationMessage, payoutPreviewValid } from '../src/payoutConfirmation.js'

test('payout confirmation names every recipient and exact USD share', () => {
  const recipients = [
    { name: 'Maintainer A', project: 'curl', amount_cents: 737 },
    { name: 'Maintainer B', project: 'openssl', amount_cents: 263 },
  ]
  assert.equal(payoutPreviewValid(1000, 2, recipients), true)
  assert.equal(payoutConfirmationMessage(1000, recipients),
    'Submit $10.00 USD in PayPal Sandbox payouts?\n\nRecipients:\nMaintainer A (curl): $7.37\nMaintainer B (openssl): $2.63')
})

test('payout is blocked if the frozen recipient count or total does not match', () => {
  assert.equal(payoutPreviewValid(1000, 1, [{ name: 'A', project: 'curl', amount_cents: 999 }]), false)
  assert.equal(payoutPreviewValid(1000, 0, []), false)
})
