import test from 'node:test'
import assert from 'node:assert/strict'
import { captureConfirmationMessage } from '../src/paymentConfirmation.js'

test('capture confirmation names the exact USD charge', () => {
  assert.equal(captureConfirmationMessage(840),
    'Capture the approved PayPal order for $8.40 USD?')
})
