import type {
  ChunkReview,
  ChunkReviewFinding,
  ChunkReviewVerdict,
  ReviewFindingSeverity,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'

interface AdvisoryReviewPanelProps {
  review?: ChunkReview | null
}

/**
 * Display-only Advisory AI Review panel (Adversarial Reviewer v1).
 *
 * Renders the read-only `ChunkStatus.review` overlay for the human reviewing a
 * chunk's result. It is advisory evidence ONLY: it has no buttons, makes no route
 * calls, wires no actions, and never approves, rejects, acknowledges, or blocks
 * anything. It must never imply the code or tests are correct, and a stale review
 * is visually demoted and never presented as current advice.
 *
 * Renders nothing when no review exists (review is null/undefined = "missing").
 */

const ADVISORY_NOTE =
  'Advisory only — does not approve, reject, or block anything.'

// Verdict copy deliberately avoids the word "approved". approve_with_notes is the
// LOW-signal verdict ("no blocking concern"), never an authorization.
const VERDICT_META: Record<
  ChunkReviewVerdict,
  { label: string; className: string; detail: string }
> = {
  approve_with_notes: {
    label: 'No blocking concern reported',
    className: 'border-slate-300 bg-slate-100 text-slate-700',
    detail:
      'The reviewer found no blocking concern, but read the notes. This does ' +
      'not confirm the code or tests are correct.',
  },
  needs_human_attention: {
    label: 'Needs human attention',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
    detail: 'A human should inspect the findings below before approving.',
  },
  risky: {
    label: 'Flagged risky',
    className: 'border-red-300 bg-red-100 text-red-800',
    detail:
      'The reviewer flagged this change as risky. This does not block anything; ' +
      'a human should review the findings carefully.',
  },
}

const SEVERITY_ORDER: ReviewFindingSeverity[] = ['high', 'warning', 'info']

const SEVERITY_META: Record<string, { label: string; className: string }> = {
  high: { label: 'High', className: 'border-red-300 bg-red-100 text-red-800' },
  warning: {
    label: 'Warning',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  info: {
    label: 'Info',
    className: 'border-slate-300 bg-slate-100 text-slate-600',
  },
}

function formatTimestamp(value?: string | null): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

function FindingRow({ finding }: { finding: ChunkReviewFinding }) {
  const severity = SEVERITY_META[finding.severity] ?? SEVERITY_META.info
  return (
    <li className="grid gap-0.5 rounded border border-slate-200 bg-white px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="outline" className={severity.className}>
          {severity.label}
        </Badge>
        <span className="text-xs text-muted-foreground">{finding.category}</span>
        <span className="font-medium">{finding.title}</span>
      </div>
      <p className="text-slate-700">{finding.explanation}</p>
      {finding.affected_files.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Files: {finding.affected_files.join(', ')}
        </p>
      )}
      {finding.suggested_human_check && (
        <p className="text-xs text-slate-600">
          Suggested check: {finding.suggested_human_check}
        </p>
      )}
    </li>
  )
}

export default function AdvisoryReviewPanel({
  review,
}: AdvisoryReviewPanelProps) {
  if (!review) return null

  const isStale = review.staleness === 'stale'
  const isCompleted = review.review_status === 'completed'
  const verdict =
    isCompleted && review.verdict ? VERDICT_META[review.verdict] : null

  const sortedFindings = [...review.findings].sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(a.severity as ReviewFindingSeverity) -
      SEVERITY_ORDER.indexOf(b.severity as ReviewFindingSeverity),
  )

  const timestamp = formatTimestamp(review.created_at)
  const metaParts = [review.provider, review.model].filter(Boolean) as string[]

  return (
    <div
      className={
        'grid gap-2 rounded border border-indigo-200 bg-indigo-50/60 px-3 py-3 text-sm text-slate-800' +
        (isStale ? ' opacity-80' : '')
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          className="border-indigo-300 bg-indigo-100 text-indigo-800"
        >
          Advisory AI Review
        </Badge>
        <Badge
          variant="outline"
          className={
            review.staleness === 'current'
              ? 'border-slate-300 bg-slate-100 text-slate-600'
              : 'border-amber-300 bg-amber-100 text-amber-800'
          }
        >
          {review.staleness}
        </Badge>
        <Badge
          variant="outline"
          className="border-slate-300 bg-slate-100 text-slate-600"
        >
          {review.review_status}
        </Badge>
        {verdict && (
          <Badge variant="outline" className={verdict.className}>
            {verdict.label}
          </Badge>
        )}
      </div>

      <p className="text-xs font-medium text-indigo-700">{ADVISORY_NOTE}</p>

      {isStale && (
        <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-800">
          This review was generated for an earlier version of this change. It is
          not current advice — re-review the latest change.
        </p>
      )}

      {!isCompleted ? (
        <p className="text-muted-foreground">
          No current advisory review is available for this chunk.
        </p>
      ) : (
        <>
          {verdict && (
            <p className="text-slate-700">{verdict.detail}</p>
          )}
          {review.summary && (
            <p className="whitespace-pre-wrap text-slate-700">{review.summary}</p>
          )}

          {sortedFindings.length > 0 && (
            <div className="grid gap-1">
              <p className="text-xs font-medium text-slate-600">
                Findings ({sortedFindings.length}) — suggestions for human review,
                not verified facts:
              </p>
              <ul className="grid gap-1">
                {sortedFindings.map((finding, index) => (
                  <FindingRow key={index} finding={finding} />
                ))}
              </ul>
            </div>
          )}

          {review.test_gap_summary && (
            <div>
              <p className="text-xs font-medium text-slate-600">Test gaps</p>
              <p className="text-slate-700">{review.test_gap_summary}</p>
            </div>
          )}
          {review.scope_summary && (
            <div>
              <p className="text-xs font-medium text-slate-600">Scope notes</p>
              <p className="text-slate-700">{review.scope_summary}</p>
            </div>
          )}
          {review.security_or_safety_summary && (
            <div>
              <p className="text-xs font-medium text-slate-600">
                Security / safety notes
              </p>
              <p className="text-slate-700">
                {review.security_or_safety_summary}
              </p>
            </div>
          )}
          {review.recommended_human_action && (
            <div>
              <p className="text-xs font-medium text-slate-600">
                Recommended human check
              </p>
              <p className="text-slate-700">{review.recommended_human_action}</p>
            </div>
          )}
        </>
      )}

      {(metaParts.length > 0 || timestamp) && (
        <p className="text-xs text-muted-foreground">
          {metaParts.join(' · ')}
          {metaParts.length > 0 && timestamp ? ' · ' : ''}
          {timestamp ?? ''}
        </p>
      )}
    </div>
  )
}
