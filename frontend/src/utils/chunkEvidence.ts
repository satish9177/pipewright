import type { ChunkReview, TestRunValidation } from '@/api/client'

// #36F / Phase 2G PR-3: read-only evidence-chip vocabulary + builder. Extracted
// verbatim from ChunkPlanPanel so the chunk panel AND the cockpit-adjacent
// "decision evidence" summary render the SAME chips from the SAME chunk data and
// can never drift. Pure module (no JSX, no actions, no behavior): the chips
// summarize signals the full RuntimeTestValidationBanner / AdvisoryReviewPanel
// still render below in full, and never soften a weak/none/risky verdict (those
// stay amber/red). Lives in a non-component .ts module so it can be shared
// without tripping react-refresh (same rationale as chunkAttention.ts).

// #36C: compact, friendly summaries for the ActiveChunkCard chips. These mirror
// the verdict vocabulary already shown in full by RuntimeTestValidationBanner /
// AdvisoryReviewPanel below; they never replace those banners.
export const ACTIVE_TEST_SUMMARY: Record<
  string,
  { label: string; className: string }
> = {
  strong: {
    label: 'Tests: strong',
    className: 'border-green-300 bg-green-100 text-green-800',
  },
  weak: {
    label: 'Tests: weak',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  none: {
    label: 'Tests: none',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  unknown: {
    label: 'Tests: unverified',
    className: 'border-slate-300 bg-slate-100 text-slate-600',
  },
}

export const ACTIVE_REVIEW_SUMMARY: Record<
  string,
  { label: string; className: string }
> = {
  approve_with_notes: {
    label: 'Review: no blocking concern',
    className: 'border-slate-300 bg-slate-100 text-slate-700',
  },
  needs_human_attention: {
    label: 'Review: needs attention',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  risky: {
    label: 'Review: risky',
    className: 'border-red-300 bg-red-100 text-red-800',
  },
}

// #36F: compact reviewer-independence chip for the evidence summary. Mirrors the
// disclosure AdvisoryReviewPanel already shows in full; 'unavailable' (no
// completed review) yields no chip so an absent review never implies independence.
export const ACTIVE_INDEPENDENCE_SUMMARY: Record<
  string,
  { label: string; className: string } | undefined
> = {
  self_review: {
    label: 'Reviewer: not independent',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  independent: {
    label: 'Reviewer: independent',
    className: 'border-emerald-300 bg-emerald-100 text-emerald-800',
  },
  unknown: {
    label: 'Reviewer: independence unverified',
    className: 'border-slate-300 bg-slate-100 text-slate-600',
  },
}

// #36F: highest-severity-first ordering and chip colors for the findings count
// chip. Mirrors the severity vocabulary AdvisoryReviewPanel renders per finding.
export const FINDING_SEVERITY_ORDER = ['high', 'warning', 'info'] as const
export const FINDING_SEVERITY_CHIP: Record<string, string> = {
  high: 'border-red-300 bg-red-100 text-red-800',
  warning: 'border-amber-300 bg-amber-100 text-amber-800',
  info: 'border-slate-300 bg-slate-100 text-slate-600',
}

// #36F: build the summary-first evidence chips for a chunk from EXISTING data
// only. Each chip mirrors a signal the full RuntimeTestValidationBanner /
// AdvisoryReviewPanel still render below; chips summarize, they never replace the
// banners and never soften a weak/none/risky verdict (those stay amber/red).
export function buildEvidenceChips(
  validation?: TestRunValidation | null,
  review?: ChunkReview | null
): Array<{ key: string; label: string; className: string }> {
  const chips: Array<{ key: string; label: string; className: string }> = []

  const testVerdict = validation?.verdict
  if (testVerdict) {
    chips.push({
      key: 'tests',
      ...(ACTIVE_TEST_SUMMARY[testVerdict] ?? ACTIVE_TEST_SUMMARY.unknown),
    })
  }

  if (review && review.review_status === 'completed') {
    if (review.verdict) {
      const meta = ACTIVE_REVIEW_SUMMARY[review.verdict]
      if (meta) chips.push({ key: 'review', ...meta })
    }
    const independence = review.reviewer_independence?.status
    if (independence) {
      const meta = ACTIVE_INDEPENDENCE_SUMMARY[independence]
      if (meta) chips.push({ key: 'independence', ...meta })
    }
    if (review.staleness === 'stale') {
      chips.push({
        key: 'stale',
        label: 'Review: stale',
        className: 'border-amber-300 bg-amber-100 text-amber-800',
      })
    }
    const findingsCount = review.findings.length
    if (findingsCount > 0) {
      const highest =
        FINDING_SEVERITY_ORDER.find(severity =>
          review.findings.some(finding => finding.severity === severity)
        ) ?? 'info'
      chips.push({
        key: 'findings',
        label: `${findingsCount} finding${findingsCount === 1 ? '' : 's'} · ${highest}`,
        className: FINDING_SEVERITY_CHIP[highest] ?? FINDING_SEVERITY_CHIP.info,
      })
    }
  }

  return chips
}
