import { useState } from 'react'
import { runsApi } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

interface RevisePlanPanelProps {
  runId: string
  // Called after a successful (or conflicting) revision so the parent can
  // refetch run/chunks/gates with the existing invalidation conventions. The
  // revised plan stays awaiting approval, so this only refreshes what is shown.
  onRevised?: () => void
}

const BLANK_MESSAGE_ERROR = 'Enter a revision request before submitting.'
const SUCCESS_MESSAGE = 'Plan revised. Review the updated chunks before approving.'

// Map a plan-turn failure to friendly, sanitized copy. The backend never leaks
// secrets, but 404 (run-not-found) and 500 details name internal modules, so
// those are never surfaced raw. 409 details (cap reached / no longer awaiting
// approval) are written user-facing on the backend and safe to show.
function getPlanTurnErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (
      error as {
        response?: { status?: number; data?: { detail?: unknown } }
      }
    ).response
    const status = response?.status
    const detail = response?.data?.detail
    const detailText =
      typeof detail === 'string' && detail.trim() ? detail.trim() : null

    // 404 = the capability flag is off (the only realistic case here, since the
    // affordance renders only for a freshly fetched plan awaiting approval). The
    // run-not-found 404 carries an internal-module detail, so never surface it.
    if (status === 404) {
      return 'Plan revision is not enabled for this run.'
    }
    // 409 = revision cap reached, or the plan is no longer awaiting approval.
    // Both details are user-facing guidance, so show them when present.
    if (status === 409) {
      return (
        detailText ??
        'This plan can no longer be revised. Refresh to see the current state.'
      )
    }
    // 422 = body validation. Blank is guarded client-side, so a 422 here means
    // the message is over the backend length cap. The detail is a Pydantic array
    // (not a string), so show friendly copy instead.
    if (status === 422) {
      return 'Your revision request is too long. Shorten it and try again.'
    }
  }
  // 500, network failures, and anything unexpected: retryable, no internal leak.
  return 'Could not revise the plan. Please try again.'
}

/**
 * §23 row 7b: small "Revise plan" affordance for a chunk plan still awaiting
 * approval. It makes the size/isolation advisories actionable: the user asks
 * Pipewright to revise the plan (e.g. "split chunk 2 into backend and frontend
 * chunks") BEFORE approving it.
 *
 * This never approves anything. On success the backend creates a new plan
 * version and the run stays awaiting approval, so the revised plan must still be
 * approved through the existing approval controls. Dormant by default: while the
 * backend PLAN_TURNS_ENABLED flag is off a submit returns 404, which is shown as
 * "Plan revision is not enabled for this run."
 */
export default function RevisePlanPanel({
  runId,
  onRevised,
}: RevisePlanPanelProps) {
  const [expanded, setExpanded] = useState(false)
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const handleSubmit = async () => {
    const trimmed = message.trim()
    if (!trimmed) {
      setSuccess(null)
      setError(BLANK_MESSAGE_ERROR)
      return
    }
    setSubmitting(true)
    setError(null)
    setSuccess(null)
    try {
      await runsApi.createPlanTurn(runId, trimmed)
      setSuccess(SUCCESS_MESSAGE)
      setMessage('')
      // Refetch so the revised chunks replace the old ones above. The plan stays
      // awaiting approval, so this panel stays mounted and the success line holds.
      onRevised?.()
    } catch (submitError: unknown) {
      setError(getPlanTurnErrorMessage(submitError))
      // Refresh anyway: a 409 ("no longer awaiting approval") means our view is
      // stale, so re-fetching self-corrects what the screen offers.
      onRevised?.()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="grid gap-3 rounded border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-700">
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-medium text-foreground">Revise plan</p>
        <Badge
          variant="outline"
          className="border-slate-300 bg-slate-100 text-slate-700"
        >
          does not approve the plan
        </Badge>
      </div>
      <p className="text-muted-foreground">
        Ask Pipewright to revise this chunk plan before you approve it. The
        revised plan will still require your approval.
      </p>

      {!expanded ? (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setExpanded(true)
              setError(null)
            }}
          >
            Revise plan
          </Button>
        </div>
      ) : (
        <>
          <Textarea
            value={message}
            onChange={event => setMessage(event.target.value)}
            placeholder="e.g. Split chunk 2 into backend and frontend chunks."
            disabled={submitting}
          />
          <div className="flex flex-wrap gap-3">
            <Button
              onClick={handleSubmit}
              disabled={submitting || message.trim().length === 0}
            >
              {submitting ? 'Revising…' : 'Revise plan'}
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                setExpanded(false)
                setMessage('')
                setError(null)
              }}
              disabled={submitting}
            >
              Cancel
            </Button>
          </div>
        </>
      )}

      {success && (
        <p className="text-sm font-medium text-green-700">{success}</p>
      )}
      {error && <p className="text-sm font-medium text-red-600">{error}</p>}
    </div>
  )
}
