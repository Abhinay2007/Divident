export type Session = {
  id: string
  mode: string
  status: 'created' | 'resolving' | 'ready' | 'failed' | string
  stage: string | null
  progress_done: number
  progress_total: number
  source_count: number
  binary_count: number | null
  error_message: string | null
}

export type RuntimeConfig = {
  paypal_env: string
  sandbox: boolean
  payments_mode: 'paypal' | 'fake'
  fee_params: { order_fee_pct: number | null; order_fee_fixed_cents: number | null; payout_fee_per_item_cents: number | null; buffer_cents: number | null; tolerance_cents: number }
}

export function getConfig() { return request<RuntimeConfig>('/config') }

export type SourceDetail = {
  source_name: string
  binary_names: string[] | null
  upstream_key: string | null
  method: string
  outcome: string
  validated: boolean
  evidence: { field: string; value: string }[] | null
  repository_url: string | null
}

export type Project = {
  upstream_key: string
  display_name: string
  repository_url: string | null
  validated: boolean
  evidence: { field: string; value: string }[]
  binary_count: number
  source_names: string[]
  payability: string
  reason_summary: string
  maintainer_name: string | null
  need_signals: {
    archived: boolean | null
    last_commit: string | null
    committers_12m: number | null
    funding_yml: boolean | null
    partial: boolean
  }
}

export type Projects = {
  projects: Project[]
  unmapped: string[]
  no_upstream: string[]
  sources: SourceDetail[]
}

export type Plan = {
  id: string
  session_id: string
  state: string
  budget_cents: number
  payable_total_cents: number
  reserved_total_cents: number
  est_payout_fees_cents: number | null
  est_incoming_fee_cents: number | null
  fee_policy_version: string | null
  charge_cents: number | null
  order_id: string | null
  order_attempt: number
  capture_attempt: number
  capture_gross_cents: number | null
  capture_fee_cents: number | null
  capture_net_cents: number | null
  failure_reason: string | null
  frozen_at: string | null
  money_summary: { charged_now_cents: number | null; pledged_cents: number; fee_estimate_cents: number | null }
  lines: {
    upstream_key: string
    display_name: string
    amount_cents: number
    payable_cents: number
    reserved_cents: number
    payability: string
    reason: string
    facts_snapshot: {
      normalized: Project['need_signals'] & { binary_count: number }
      sources: {
        source_name: string
        repository_url: string | null
        resolution: { method: string; evidence: { field: string; value: string }[] | null }
      }[]
    } | null
  }[]
}

export type ProjectExplanation = {
  upstream_key: string
  status: string
  claims: { id: string; text: string; evidence_ids: string[] }[]
  evidence: { id: string; [key: string]: unknown }[]
}

export function getPlanExplanations(id: string) {
  return request<{ plan_id: string; explanations: ProjectExplanation[] }>(
    `/api/plans/${encodeURIComponent(id)}/explanations`, { headers: planHeaders(id) },
  )
}

export class ApiError extends Error {
  constructor(message: string, readonly requestId?: string, readonly status?: number, readonly code?: string) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : body.message
    const code = typeof body.code === 'string' ? body.code : undefined
    const knownMessage = code === 'allocation_error'
      ? 'The allocation rules could not fit this budget. Choose another amount and try again.'
      : code === 'charge_unavailable'
        ? 'Approval is unavailable because the required payment fee inputs are not measured. No order was created.'
        : code === 'order_outcome_unknown'
          ? 'The order result is uncertain. Check the plan status before taking another payment action.'
          : code === 'no_failed_payout_items'
            ? 'There are no failed payout items eligible for retry.'
          : undefined
    throw new ApiError(
      knownMessage || (typeof detail === 'string' ? detail : `Request failed (${response.status}). Try again.`),
      typeof body.request_id === 'string' ? body.request_id : undefined,
      response.status,
      code,
    )
  }
  return body as T
}

export function createDemoSession() {
  return request<Session>('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'demo' }),
  })
}

export function createUploadSession(payload: unknown) {
  return request<Session>('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'upload', payload }),
  })
}

export function getSession(id: string) {
  return request<Session>(`/api/sessions/${encodeURIComponent(id)}`)
}

export function getProjects(id: string) {
  return request<Projects>(`/api/sessions/${encodeURIComponent(id)}/projects`)
}

export function createPlan(sessionId: string, budgetCents: number, evaluationDate: string) {
  return request<{ id: string; action_token: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/plans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ budget_cents: budgetCents, evaluation_date: evaluationDate }),
  })
}

function planHeaders(id: string): HeadersInit {
  const token = sessionStorage.getItem(`plan:${id}`)
  return token ? { 'X-Plan-Token': token } : {}
}

export function getPlan(id: string) {
  return request<Plan>(`/api/plans/${encodeURIComponent(id)}`, { headers: planHeaders(id) })
}

export function adjustPlanBudget(id: string, budgetCents: number, evaluationDate: string) {
  return request<Plan>(`/api/plans/${encodeURIComponent(id)}/budget`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...planHeaders(id) },
    body: JSON.stringify({ budget_cents: budgetCents, evaluation_date: evaluationDate }),
  })
}

export function approvePlan(id: string) {
  return request<{ order_id: string; approval_url: string }>(`/api/plans/${encodeURIComponent(id)}/approve`, {
    method: 'POST', headers: planHeaders(id),
  })
}

export type Capture = {
  capture: { capture_id: string; gross_cents: number; fee_cents: number | null; net_cents: number; currency: string; status: string }
  state: string
}

export function capturePlan(id: string) {
  return request<Capture>(`/api/plans/${encodeURIComponent(id)}/capture`, { method: 'POST', headers: planHeaders(id) })
}

export type PayoutItem = {
  sender_item_id: string
  upstream_key: string
  recipient_display: string
  amount_cents: number
  status: string
  failure_reason: string | null
  updated_at: string
}

export type PayoutBatch = {
  id: string
  attempt: number
  status: string
  fees_cents: number | null
  items: PayoutItem[]
}

export type Payouts = { batches: PayoutBatch[] }
export type PayoutAction = { batch_id: string; item_count: number; state: string }

export function getPayouts(id: string) {
  return request<Payouts>(`/api/plans/${encodeURIComponent(id)}/payouts`, { headers: planHeaders(id) })
}

export function dispatchPayouts(id: string) {
  return request<PayoutAction>(`/api/plans/${encodeURIComponent(id)}/payout`, { method: 'POST', headers: planHeaders(id) })
}

export function retryFailedPayouts(id: string) {
  return request<PayoutAction>(`/api/plans/${encodeURIComponent(id)}/payout/retry`, { method: 'POST', headers: planHeaders(id) })
}

export function reconcilePayouts(id: string) {
  return request<Payouts>(`/api/plans/${encodeURIComponent(id)}/reconcile`, { method: 'POST', headers: planHeaders(id) })
}
