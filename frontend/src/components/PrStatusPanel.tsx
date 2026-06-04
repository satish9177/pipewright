import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import {
  runsApi,
  type ChecksState,
  type PrChecksSummary,
  type PrState,
  type PrStatus,
  type Project,
  type Run,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

interface PrStatusPanelProps {
  run: Run
  project?: Project
}

function getErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } })
      .response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return fallback
}

const PR_STATE_LABEL: Record<PrState, string> = {
  not_started: 'Not started',
  ready_to_push: 'Ready to push',
  local_ready: 'Ready (local-only)',
  local_complete: 'Completed locally',
  pushing: 'Pushing…',
  push_failed: 'Push failed',
  pr_open: 'PR open',
  unknown: 'Unknown',
}

const PR_STATE_CLASS: Record<PrState, string> = {
  not_started: 'text-muted-foreground',
  ready_to_push: 'text-blue-600 border-blue-200',
  local_ready: 'text-blue-600 border-blue-200',
  local_complete: 'text-green-600 border-green-200',
  pushing: 'text-amber-600 border-amber-200',
  push_failed: 'text-red-600 border-red-200',
  pr_open: 'text-green-600 border-green-200',
  unknown: 'text-muted-foreground',
}

const CHECKS_LABEL: Record<ChecksState, string> = {
  unknown: 'Checks unknown',
  pending: 'Checks pending',
  passed: 'Checks passed',
  failed: 'Checks failed',
  unavailable: 'Checks unavailable',
  no_checks: 'No checks configured',
}

const CHECKS_CLASS: Record<ChecksState, string> = {
  unknown: 'text-muted-foreground',
  pending: 'text-amber-600 border-amber-200',
  passed: 'text-green-600 border-green-200',
  failed: 'text-red-600 border-red-200',
  unavailable: 'text-muted-foreground border-muted-foreground/30',
  no_checks: 'text-muted-foreground border-muted-foreground/30',
}

// Mirror of the backend derive_pr_state (#31B) so the PR state is shown on load
// WITHOUT any network call. The explicit refresh below replaces this with the
// authoritative pr_state from the endpoint when the user asks for it.
function derivePrState(run: Run, prMode?: string): PrState {
  if (run.pr_url) return 'pr_open'
  if (run.status === 'pushing') return 'pushing'
  if (run.status === 'push_failed') return 'push_failed'
  if (prMode === 'local_only') {
    if (run.status === 'complete') return 'local_complete'
    if (run.status === 'final_approved') return 'local_ready'
    return 'not_started'
  }
  if (run.status === 'final_approved') return 'ready_to_push'
  if (run.status === 'complete') return 'unknown'
  return 'not_started'
}

function ChecksSummaryView({ checks }: { checks: PrChecksSummary }) {
  const label = CHECKS_LABEL[checks.state] ?? CHECKS_LABEL.unknown
  const className = CHECKS_CLASS[checks.state] ?? CHECKS_CLASS.unknown
  const showCounts =
    checks.state === 'passed' ||
    checks.state === 'failed' ||
    checks.state === 'pending'

  return (
    <div className="grid gap-1">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className={className}>
          {label}
        </Badge>
        {showCounts && (
          <span className="text-xs text-muted-foreground">
            {checks.passed} passed · {checks.failed} failed · {checks.pending}{' '}
            pending
            {checks.skipped ? ` · ${checks.skipped} skipped` : ''} ·{' '}
            {checks.total} total
          </span>
        )}
      </div>
      {checks.state === 'unavailable' && (
        <p className="text-xs text-muted-foreground">
          Checks could not be retrieved from GitHub right now. This is not a
          failing build — try refreshing again.
        </p>
      )}
      {checks.checked_at && (
        <p className="text-xs text-muted-foreground">
          Last refreshed {new Date(checks.checked_at).toLocaleString()}
        </p>
      )}
    </div>
  )
}

export default function PrStatusPanel({ run, project }: PrStatusPanelProps) {
  // The last explicitly-refreshed payload, if any. Null until the user clicks.
  const [refreshed, setRefreshed] = useState<PrStatus | null>(null)
  const [checksError, setChecksError] = useState<string | null>(null)

  const refreshMutation = useMutation({
    mutationFn: () => runsApi.getPrStatus(run.id),
    onSuccess: (data) => {
      setRefreshed(data)
      setChecksError(null)
    },
    onError: (error: unknown) => {
      setChecksError(getErrorMessage(error, 'Failed to refresh PR checks.'))
    },
  })

  const hasPr = Boolean(run.pr_url)
  // Prefer the authoritative state from an explicit refresh; otherwise derive
  // it locally so the panel renders on load without calling GitHub.
  const prState: PrState =
    refreshed?.pr_state ?? derivePrState(run, project?.pr_mode)
  const checks = refreshed?.checks ?? null

  return (
    <Card className="mb-4">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">PR Status</CardTitle>
            <CardDescription>
              Pull request lifecycle and GitHub checks. Display-only — refreshing
              checks never pushes, merges, or changes approvals.
            </CardDescription>
          </div>
          <Badge variant="outline" className={PR_STATE_CLASS[prState]}>
            {PR_STATE_LABEL[prState]}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <p className="font-medium">Branch</p>
            <p className="text-muted-foreground break-words">
              {run.branch_name || 'Not available yet'}
            </p>
          </div>
          <div>
            <p className="font-medium">PR Number</p>
            <p className="text-muted-foreground">
              {run.pr_number ? `#${run.pr_number}` : 'Not created yet'}
            </p>
          </div>
          <div>
            <p className="font-medium">Pull Request</p>
            {run.pr_url ? (
              <a
                href={run.pr_url}
                target="_blank"
                rel="noreferrer"
                className="text-blue-600 underline break-words"
              >
                Open on GitHub
              </a>
            ) : (
              <p className="text-muted-foreground">Not created yet</p>
            )}
          </div>
        </div>

        {hasPr && (
          <div className="grid gap-2">
            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => refreshMutation.mutate()}
                disabled={refreshMutation.isPending}
                className="w-fit"
              >
                {refreshMutation.isPending
                  ? 'Refreshing…'
                  : 'Refresh PR checks'}
              </Button>
              {!checks && !refreshMutation.isPending && (
                <span className="text-xs text-muted-foreground">
                  Checks are not fetched automatically. Click to refresh.
                </span>
              )}
            </div>
            {checks && <ChecksSummaryView checks={checks} />}
            {checksError && (
              <p className="text-sm font-medium text-red-500">{checksError}</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
