export type PayPalReturnState = 'return' | 'cancel' | null

export function planTokenKey(planId: string): string
export function planHeaders(
  planId: string,
  storage?: Pick<Storage, 'getItem' | 'setItem'>,
  legacyStorage?: Pick<Storage, 'getItem'>,
): HeadersInit
export function paypalReturnState(search: string): PayPalReturnState
