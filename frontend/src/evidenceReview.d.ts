export type EvidenceReviewPayload = {
  status: 'available' | 'unavailable' | 'invalid'
  reason?: string | null
  summary?: string | null
  contributors?: { contributor_id: string; observation: string; cited_pr_numbers: number[]; merged_pr_count: number; reviewed_pr_count: number }[]
  cited_pr_numbers?: number[]
  citations?: { contributor_id: string; activity_type: 'merged_pull_request' | 'code_review'; pr_number: number; url: string }[]
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
  contributorGroups: { label: string; contributors: (NonNullable<EvidenceReviewPayload['contributors']>[number] & {
    citations: NonNullable<EvidenceReviewPayload['citations']>
  })[] }[]
  citedPrNumbers: NonNullable<EvidenceReviewPayload['cited_pr_numbers']>
  citations: NonNullable<EvidenceReviewPayload['citations']>
  advisoryFlags: NonNullable<EvidenceReviewPayload['advisory_flags']>
  model: string | null | undefined
} | null
