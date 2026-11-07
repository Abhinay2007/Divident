export type Session = {
  id: string
  status: 'created' | 'resolving' | 'ready' | 'failed' | string
  stage: string | null
  progress_done: number
  progress_total: number
  source_count: number
  binary_count: number | null
  error_message: string | null
}

export type SourceDetail = {
  source_name: string
  binary_names: string[] | null
  upstream_key: string | null
  method: string
  outcome: string
}

export type Project = {
  upstream_key: string
  display_name: string
  repository_url: string | null
  binary_count: number
  source_names: string[]
  payability: string
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
  lines: { upstream_key: string; display_name: string; amount_cents: number; payable_cents: number; reserved_cents: number; reason: string }[]
}

export class ApiError extends Error {
  constructor(message: string, readonly requestId?: string, readonly status?: number) {
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
    throw new ApiError(
      typeof detail === 'string' ? detail : code === 'allocation_error'
        ? 'The allocation rules could not distribute this budget. Increase it and retry.'
        : `Request failed (${response.status}). Try again.`,
      typeof body.request_id === 'string' ? body.request_id : undefined,
      response.status,
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

export function getSession(id: string) {
  return request<Session>(`/api/sessions/${encodeURIComponent(id)}`)
}

export function getProjects(id: string) {
  return request<Projects>(`/api/sessions/${encodeURIComponent(id)}/projects`)
}

export function createPlan(sessionId: string, budgetCents: number, evaluationDate: string) {
  return request<{ id: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/plans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ budget_cents: budgetCents, evaluation_date: evaluationDate }),
  })
}

export function getPlan(id: string) {
  return request<Plan>(`/api/plans/${encodeURIComponent(id)}`)
}
