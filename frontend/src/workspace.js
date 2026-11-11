export function continuePath(state, planId) {
  if (state === 'draft') return `/allocation/${planId}`
  if (['funded', 'paying', 'settled', 'settled_with_exceptions'].includes(state)) return `/payouts/${planId}`
  return `/fund/${planId}`
}

export function hasWorkspaceActivity(sessions) {
  return sessions.length > 0
}
