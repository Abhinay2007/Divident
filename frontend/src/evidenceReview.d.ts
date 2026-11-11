export type EvidenceReviewPayload = {
  status: 'available' | 'unavailable' | 'invalid'
  reason?: string | null
  summary?: string | null
  contributors?: { contributor_id: string; observation: string; cited_pr_numbers: number[] }[]
  cited_pr_numbers?: number[]
  citations?: { pr_number: number; url: string }[]
  advisory_flags?: string[]
  model?: string | null
}

export function requestEvidenceReview<T>(
  planId: string,
  headers: HeadersInit,
  request: (url: string, options: RequestInit) => Promise<T>,
): Promise<T>

export function evidenceReviewPresentation(review?: EvidenceReviewPayload | null): {
  status: 'unavailable' | 'invalid'
  reason: string | null
} | {
  status: 'available'
  summary: string | null | undefined
  contributors: NonNullable<EvidenceReviewPayload['contributors']>
  citedPrNumbers: NonNullable<EvidenceReviewPayload['cited_pr_numbers']>
  citations: NonNullable<EvidenceReviewPayload['citations']>
  advisoryFlags: NonNullable<EvidenceReviewPayload['advisory_flags']>
  model: string | null | undefined
} | null
