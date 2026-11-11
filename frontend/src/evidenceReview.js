export function requestEvidenceReview(planId, headers, request) {
  return request(`/api/plans/${encodeURIComponent(planId)}/evidence-review`, { headers })
}

export function evidenceReviewPresentation(review) {
  if (!review) return null
  if (review.status !== 'available') {
    return {
      status: review.status === 'unavailable' ? 'unavailable' : 'invalid',
      reason: review.reason ?? null,
    }
  }
  const citations = (review.citations ?? []).filter((citation) => {
    try {
      const url = new URL(citation.url)
      return url.protocol === 'https:' && url.hostname === 'github.com'
        && new RegExp(`/pull/${citation.pr_number}$`).test(url.pathname)
    } catch {
      return false
    }
  })
  return {
    status: 'available',
    summary: review.summary,
    contributors: review.contributors,
    citedPrNumbers: review.cited_pr_numbers,
    citations,
    advisoryFlags: review.advisory_flags,
    model: review.model,
  }
}
