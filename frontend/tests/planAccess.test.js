import test from 'node:test'
import assert from 'node:assert/strict'
import { planHeaders, planTokenKey, paypalReturnState } from '../src/planAccess.js'

test('PayPal return uses only the plan token already stored for that plan', () => {
  const storage = new Map([[planTokenKey('plan-1'), 'dividend-capability']])
  const sessionStorage = { getItem: (key) => storage.get(key) ?? null }
  const search = '?paypal=return&token=PAYPAL_ORDER_ID&PayerID=BUYER_ID'

  assert.equal(paypalReturnState(search), 'return')
  assert.deepEqual(planHeaders('plan-1', sessionStorage), {
    'X-Plan-Token': 'dividend-capability',
  })
  assert.deepEqual(planHeaders('plan-2', sessionStorage), {})
})

test('refresh retains session authorization and does not exchange PayPal query values', () => {
  const data = new Map([['plan:plan-1', 'stored-capability']])
  const localStorage = { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) }
  const search = '?paypal=return&token=PAYPAL_ORDER_ID&PayerID=BUYER_ID'

  paypalReturnState(search)
  assert.deepEqual(planHeaders('plan-1', localStorage), {
    'X-Plan-Token': 'stored-capability',
  })
  assert.deepEqual(planHeaders('plan-1', localStorage), { 'X-Plan-Token': 'stored-capability' })
})

test('existing session token migrates to persistent same-origin storage for another tab', () => {
  const saved = new Map()
  const persistent = { getItem: (key) => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) }
  const legacy = { getItem: (key) => key === 'plan:plan-1' ? 'legacy-capability' : null }

  assert.deepEqual(planHeaders('plan-1', persistent, legacy), {
    'X-Plan-Token': 'legacy-capability',
  })
  assert.equal(persistent.getItem('plan:plan-1'), 'legacy-capability')
})

test('cancel return is recognized without authorizing from the PayPal order token', () => {
  assert.equal(paypalReturnState('?paypal=cancel&token=PAYPAL_ORDER_ID'), 'cancel')
  assert.deepEqual(planHeaders('plan-1', { getItem: () => null }), {})
  assert.equal(paypalReturnState('?paypal=unexpected&token=PAYPAL_ORDER_ID'), null)
})
