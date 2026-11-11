export function planTokenKey(planId) {
  return `plan:${planId}`
}

export function planHeaders(planId, storage = globalThis.localStorage, legacyStorage = globalThis.sessionStorage) {
  const key = planTokenKey(planId)
  const token = storage?.getItem(key) || legacyStorage?.getItem(key)
  if (token && storage && !storage.getItem(key)) storage.setItem(key, token)
  return token ? { 'X-Plan-Token': token } : {}
}

export function paypalReturnState(search) {
  const state = new URLSearchParams(search).get('paypal')
  return state === 'return' || state === 'cancel' ? state : null
}
