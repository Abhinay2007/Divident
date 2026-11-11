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
  const citations = []
  const seenCitations = new Set()
  for (const citation of review.citations ?? []) {
    try {
      const url = new URL(citation.url)
      if (url.protocol !== 'https:' || url.hostname !== 'github.com'
        || !new RegExp(`/pull/${citation.pr_number}$`).test(url.pathname)) continue
      const key = `${citation.contributor_id}:${citation.activity_type}:${citation.pr_number}:${citation.url}`
      if (!seenCitations.has(key)) {
        seenCitations.add(key)
        citations.push(citation)
      }
    } catch {
      continue
    }
  }
  const groups = new Map()
  for (const contributor of review.contributors ?? []) {
    const sources = citations.filter((citation) => citation.contributor_id === contributor.contributor_id
      && contributor.cited_pr_numbers.includes(citation.pr_number))
    const merged = contributor.merged_pr_count
    const reviewed = contributor.reviewed_pr_count
    const label = merged && reviewed ? 'Merged work and reviews'
      : reviewed ? 'Review-focused participation'
        : merged === 1 ? 'Verified merged contribution'
          : merged ? 'Merged contributions' : 'Other contributor activity'
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push({ ...contributor, citations: sources })
  }
  return {
    status: 'available',
    summary: review.summary,
    contributors: review.contributors,
    contributorGroups: [...groups].map(([label, contributors]) => ({ label, contributors })),
    citedPrNumbers: review.cited_pr_numbers,
    citations,
    advisoryFlags: review.advisory_flags,
    model: review.model,
  }
}
