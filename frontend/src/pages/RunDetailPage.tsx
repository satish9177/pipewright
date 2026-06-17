import { useState, type ReactNode } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  runsApi,
  gatesApi,
  projectsApi,
  memoryApi,
  ApprovalGate,
  isNoRunResponse,
  isStaleIndexResponse,
  isStartContextDriftedResponse,
} from '@/api/client'
import type {
  ChunkPlanResponse,
  OperatorAction,
  Project,
  Run,
  RunStatus,
  RunMemorySuggestionGenerateResponse,
  StartContextDriftedResponse,
  TestRunVerdict,
} from '@/api/client'
import {
  coEqualActionHandlerKey,
  primaryActionHandlerKey,
} from '@/lib/operatorPrimaryAction'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import RunStatusBadge from '@/components/RunStatusBadge'
import EventLog from '@/components/EventLog'
import RunTimeline from '@/components/RunTimeline'
import ChunkPlanPanel from '@/components/ChunkPlanPanel'
import { chunkNeedsAttention } from '@/utils/chunkAttention'
import OperatorAttentionPanel from '@/components/OperatorAttentionPanel'
import FinalApprovalPanel from '@/components/FinalApprovalPanel'
import TestValidationAckPanel from '@/components/TestValidationAckPanel'
import ReviewFindingsAckPanel from '@/components/ReviewFindingsAckPanel'
import MemoryConflictPanel from '@/components/MemoryConflictPanel'
import PushPrPanel from '@/components/PushPrPanel'
import PrStatusPanel from '@/components/PrStatusPanel'
import ProviderDiagnosticsPanel from '@/components/ProviderDiagnosticsPanel'
import RunMemoryProvenancePanel from '@/components/RunMemoryProvenancePanel'
import RunMemorySuggestionsDigest from '@/components/RunMemorySuggestionsDigest'
import RunSafetyStrip from '@/components/RunSafetyStrip'
import TestCommandQualityWarning from '@/components/TestCommandQualityWarning'
import ReportView from '@/components/ReportView'
import PlanView from '@/components/PlanView'
import useRunEvents from '@/hooks/useRunEvents'
import useRunTimeline from '@/hooks/useRunTimeline'
import type { RunViewMode } from '@/types/viewMode'

const RUN_VIEW_MODE_STORAGE_KEY = 'pipewright.runDetail.viewMode'

// localStorage can throw (private-mode quotas, disabled storage, sandboxed
// iframes). The view-mode preference is non-essential, so any failure falls
// back to Plain English on read and is silently ignored on write rather than
// crashing the run detail page.
function readStoredRunViewMode(): RunViewMode {
  if (typeof window === 'undefined') return 'plain'
  try {
    return window.localStorage.getItem(RUN_VIEW_MODE_STORAGE_KEY) === 'developer'
      ? 'developer'
      : 'plain'
  } catch {
    return 'plain'
  }
}

function writeStoredRunViewMode(mode: RunViewMode) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(RUN_VIEW_MODE_STORAGE_KEY, mode)
  } catch {
    // Preference persistence is best-effort; ignore storage failures.
  }
}

// #37A: the pipeline stages, in order, with plain display labels. The `key`
// values match the backend's run.current_step vocabulary (same set the old
// StepIndicator keyed off); the `label` is the friendly stage name shown to the
// user (e.g. `approval` -> "Review", `github_pr` -> "Ship").
const PIPELINE_STAGES: { key: string; label: string }[] = [
  { key: 'plan', label: 'Plan' },
  { key: 'code', label: 'Code' },
  { key: 'patch', label: 'Patch' },
  { key: 'test', label: 'Test' },
  { key: 'approval', label: 'Review' },
  { key: 'github_pr', label: 'Ship' },
]

// Statuses where the system is actively working — used only to word the active
// stage caption ("in progress" vs "current"). Cosmetic; gates nothing.
const WORKING_STATUSES = new Set(['running', 'running_chunks', 'pushing'])

// #37A: compact "Chunk N of M" summary for the header meta row. Returns null when
// no chunk plan exists yet, so the header stays clean for non-chunked/early runs.
function chunkSummaryText(run: Run): string | null {
  if (run.total_chunks == null) return null
  if (run.current_chunk_number == null) {
    return `${run.total_chunks} ${run.total_chunks === 1 ? 'chunk' : 'chunks'}`
  }
  return `Chunk ${run.current_chunk_number} of ${run.total_chunks}`
}

// #37A: calm, display-only pipeline rail. It replaces the old StepIndicator
// (blue boxes + ">" separators) and mirrors the exact same run.current_step /
// run.status derivation, so it is a pure presentation change: stages before the
// current one read as done, the matching step is highlighted, a failed run marks
// its current stage, and a complete run marks every stage done. No data, wiring,
// or behavior changes.
function PipelineRail({
  currentStep,
  status,
}: {
  currentStep: string | null
  status: string
}) {
  const currentIndex = currentStep
    ? PIPELINE_STAGES.findIndex(stage => stage.key === currentStep)
    : -1
  const isComplete = status === 'complete'

  return (
    <div
      className="mb-6 flex items-stretch overflow-hidden rounded-lg border bg-[var(--pw-bg-elev)]"
      aria-label="Pipeline progress"
    >
      {PIPELINE_STAGES.map((stage, index) => {
        const isDone = isComplete || (currentIndex >= 0 && index < currentIndex)
        const isCurrent = stage.key === currentStep && !isComplete
        const isFailed = status === 'failed' && isCurrent

        const dotClass = isFailed
          ? 'bg-red-500'
          : isCurrent
            ? 'bg-amber-400'
            : isDone
              ? 'bg-emerald-500'
              : 'border border-muted-foreground/40 bg-transparent'

        const labelClass = isFailed
          ? 'text-red-600'
          : isCurrent
            ? 'text-background'
            : isDone
              ? 'text-muted-foreground'
              : 'text-muted-foreground/60'

        const cellClass = isFailed
          ? 'bg-red-50'
          : isCurrent
            ? 'bg-foreground'
            : ''

        const caption = isFailed
          ? 'failed'
          : WORKING_STATUSES.has(status)
            ? 'in progress'
            : 'current'

        return (
          <div
            key={stage.key}
            className={`flex min-w-0 flex-1 flex-col gap-1 px-3 py-2.5 ${index > 0 ? 'border-l' : ''} ${cellClass}`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotClass}`} />
              <span
                className={`truncate text-[11px] font-medium uppercase tracking-wide ${labelClass}`}
              >
                {stage.label}
              </span>
            </div>
            {isCurrent && (
              <span
                className={`pl-4 font-mono text-[10px] ${isFailed ? 'text-red-600' : 'text-background/70'}`}
              >
                {caption}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function getErrorMessage(error: unknown, fallback: string) {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    const response = (error as { response?: { data?: { detail?: unknown } } })
      .response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }

  return fallback
}

function getResponseData(error: unknown): unknown {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    return (error as { response?: { data?: unknown } }).response?.data
  }
  return undefined
}

function noRunHandoffMessage(data: unknown): string {
  if (!isNoRunResponse(data)) return 'Failed to start implementation.'
  const backendMessage = data.message ? `${data.message} ` : ''
  if (isStaleIndexResponse(data)) {
    return `${backendMessage}Re-index the repository, then submit again.`
  }
  if (data.current_branch == null) {
    return `${backendMessage}Detached HEAD is not a safe start point. Checkout the branch you want Pipewright to start from, then submit again.`
  }
  return `${backendMessage}Checkout the branch you want Pipewright to start from, then submit again.`
}

// Human-readable copy for the backend's stable retry_ineligible reasons (#26E2).
// Backend reason identifiers come from backend/pipeline/patch_failures.py.
const RETRY_INELIGIBLE_MESSAGES: Record<string, string> = {
  stale_failure_report_id:
    'This failure report is stale. Refresh the run and try again.',
  dirty_worktree: 'The working tree is dirty. Clean it before retrying.',
  disallowed_failure_type:
    'This failure type cannot be retried automatically.',
  missing_or_malformed_report:
    'The patch failure report is missing or malformed.',
  missing_failure_report_id:
    'The patch failure report is missing or malformed.',
  dependencies_not_met: "This chunk's dependencies are not completed.",
  human_retry_cap_exhausted: 'The retry limit has been reached.',
  chunk_not_failed: 'Retry is only available while the chunk is in a failed state.',
}

// The default chunk-approval gate summary (backend) ends with this sentence. It
// is only honest when runtime validation was strong; for weak/none/unknown or a
// missing verdict we rewrite just that sentence for display. Custom/AI summaries
// (e.g. high-risk gates) never contain it, so they pass through unchanged.
const AUTO_TESTS_PASSED_SENTENCE =
  'Tests have passed. Commit is pending human approval.'

function approvalSummaryForDisplay(
  summary: string,
  verdict?: TestRunVerdict | null
): string {
  if (verdict === 'strong') return summary
  return summary.replace(
    AUTO_TESTS_PASSED_SENTENCE,
    'Meaningful test validation was not confirmed. Commit is pending human approval.'
  )
}

function retrySuccessMessage(status: string): string {
  if (status === 'awaiting_chunk_approval') {
    // Neutral: the retry applied the change, but the command exiting 0 does not
    // mean meaningful tests ran. The recovered-patch marker shows the verdict.
    return 'Retry applied the change. Review the recovered patch before committing.'
  }
  if (status === 'failed') {
    return 'Retry ran but the patch failed again.'
  }
  return 'Retry completed.'
}

// Map a retry error to inline copy. retry_ineligible bodies (409/422) carry a
// stable `reason` and sometimes a `detail`; wrong_branch always carries a
// ready-to-show detail. Non-ineligible HTTP errors fall back to the shared
// getErrorMessage handler.
function getRetryErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const data = (
      error as {
        response?: {
          data?: {
            status?: unknown
            reason?: unknown
            detail?: unknown
          }
        }
      }
    ).response?.data
    if (data && data.status === 'retry_ineligible') {
      const reason = typeof data.reason === 'string' ? data.reason : ''
      const detail = typeof data.detail === 'string' ? data.detail : undefined
      if (reason === 'wrong_branch') {
        return detail || 'Checkout the run branch and try again.'
      }
      const mapped = RETRY_INELIGIBLE_MESSAGES[reason]
      if (mapped) return mapped
      return detail || (reason ? reason.replace(/_/g, ' ') : 'Retry is not allowed right now.')
    }
  }
  return getErrorMessage(error, 'Failed to retry chunk.')
}

const TERMINAL_RUN_STATUSES: RunStatus[] = ['complete', 'failed', 'rejected']

type RunContextRailMode = 'working' | 'review' | 'pr'
type RailOperatorState = NonNullable<ChunkPlanResponse['operator_state']>
type RailFact = {
  id: string
  label: string
  detail: string
}

const HUMAN_REVIEW_STATUSES = new Set<RunStatus>([
  'awaiting_chunk_plan_approval',
  'awaiting_chunk_approval',
  'awaiting_final_approval',
  'awaiting_memory_conflict_approval',
  'awaiting_scope_approval',
  'paused',
])

const TERMINAL_NO_RAIL_STATUSES = new Set<RunStatus>([
  'complete',
  'failed',
  'rejected',
  'final_rejected',
  'push_failed',
])

function getRunContextRailMode(
  run: Run,
  operatorState?: RailOperatorState | null,
): RunContextRailMode | null {
  if (run.status === 'complete' && run.pr_url) return 'pr'
  if (!operatorState || TERMINAL_NO_RAIL_STATUSES.has(run.status)) return null
  if (
    operatorState.waiting_on === 'system' ||
    operatorState.phase === 'working' ||
    WORKING_STATUSES.has(run.status)
  ) {
    return 'working'
  }
  if (
    operatorState.waiting_on === 'human' ||
    operatorState.phase === 'waiting_for_you' ||
    HUMAN_REVIEW_STATUSES.has(run.status)
  ) {
    return 'review'
  }
  return null
}

function matchesRailFact(
  candidate: { id?: string; label?: string },
  patterns: string[],
) {
  const text = `${candidate.id ?? ''} ${candidate.label ?? ''}`.toLowerCase()
  return patterns.some(pattern => text.includes(pattern))
}

function findTrustFact(
  operatorState: RailOperatorState,
  patterns: string[],
): RailFact | null {
  const fact = (operatorState.trust_facts ?? []).find(candidate =>
    matchesRailFact(candidate, patterns),
  )
  if (!fact) return null
  return {
    id: fact.id,
    label: fact.label,
    detail: fact.detail,
  }
}

function summarizeTrustOrSafety(
  operatorState: RailOperatorState,
  label: string,
  patterns: string[],
): RailFact {
  const fact = findTrustFact(operatorState, patterns)
  if (fact) return { ...fact, label }

  const check = (operatorState.safety_checks ?? []).find(candidate =>
    matchesRailFact(candidate, patterns),
  )
  if (check) {
    return {
      id: check.id,
      label,
      detail: `${check.status.replace(/_/g, ' ')}: ${check.detail}`,
    }
  }

  return {
    id: label.toLowerCase().replace(/\s+/g, '_'),
    label,
    detail: 'Not reported yet.',
  }
}

function pushedSoFarFact(
  run: Run,
  operatorState: RailOperatorState,
): RailFact {
  const fact = findTrustFact(operatorState, ['push', 'pushed', 'pull request'])
  if (fact) return { ...fact, label: 'Pushed so far' }

  if (run.pr_url) {
    return {
      id: 'pushed_so_far',
      label: 'Pushed so far',
      detail: 'A pull request is already recorded.',
    }
  }
  if (run.pushed_at || run.pr_created_at) {
    return {
      id: 'pushed_so_far',
      label: 'Pushed so far',
      detail: 'A push or pull request is already recorded.',
    }
  }
  if (run.push_error) {
    return {
      id: 'pushed_so_far',
      label: 'Pushed so far',
      detail: 'A previous push attempt failed. See Finish & ship.',
    }
  }
  return {
    id: 'pushed_so_far',
    label: 'Pushed so far',
    detail: 'Nothing recorded.',
  }
}

function RailFactList({ facts }: { facts: RailFact[] }) {
  return (
    <dl className="grid gap-2">
      {facts.map(fact => (
        <div key={fact.id} className="grid gap-0.5">
          <dt className="font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            {fact.label}
          </dt>
          <dd className="text-sm leading-snug text-foreground">{fact.detail}</dd>
        </div>
      ))}
    </dl>
  )
}

function RunContextRail({
  run,
  operatorState,
  project,
}: {
  run: Run
  operatorState?: RailOperatorState | null
  project?: Project
}) {
  const mode = getRunContextRailMode(run, operatorState)
  if (!mode) return null
  if (mode === 'pr') {
    return <PrStatusPanel run={run} project={project} />
  }
  if (!operatorState) return null

  const trustFacts = (operatorState.trust_facts ?? []).map(fact => ({
    id: fact.id,
    label: fact.label,
    detail: fact.detail,
  }))
  const pushedFact = pushedSoFarFact(run, operatorState)
  const waitingOnFact: RailFact = {
    id: 'waiting_on',
    label: 'Waiting on',
    detail:
      operatorState.waiting_on === 'system'
        ? 'Pipewright / system'
        : operatorState.waiting_on === 'human'
          ? 'You'
          : 'Nobody',
  }

  const reviewFacts: RailFact[] = [
    summarizeTrustOrSafety(operatorState, 'Scope', ['scope']),
    summarizeTrustOrSafety(operatorState, 'Tests', ['test', 'validation']),
    summarizeTrustOrSafety(operatorState, 'Review', ['review']),
    pushedFact,
  ]

  return (
    <Card className="mb-6 bg-[var(--pw-bg-elev)]">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <p className="font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Context rail
          </p>
          <Badge variant="outline" className="text-[10px]">
            Read-only
          </Badge>
        </div>
        <CardTitle className="text-base">
          {mode === 'working' ? 'While you wait' : 'Before you approve'}
        </CardTitle>
        <CardDescription>
          {mode === 'working'
            ? 'Supplementary run context while Pipewright is working.'
            : 'Existing trust signals for the approval decision.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {mode === 'working' ? (
          <>
            <RailFactList
              facts={[
                waitingOnFact,
                {
                  id: 'will_pause',
                  label: 'Will pause',
                  detail:
                    'Pipewright stops at required human approval or review gates before moving on.',
                },
                pushedFact,
              ]}
            />
            {trustFacts.length > 0 && (
              <div className="border-t pt-3">
                <p className="mb-2 font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Trust facts
                </p>
                <RailFactList facts={trustFacts} />
              </div>
            )}
            <p className="rounded-lg border border-dashed px-3 py-2 text-xs leading-relaxed text-muted-foreground">
              Pipewright does not commit, push, create pull requests, or merge
              without the required gate for that step. Merge is always manual.
            </p>
          </>
        ) : (
          <>
            <RailFactList facts={reviewFacts} />
            {trustFacts.length > 0 && (
              <div className="border-t pt-3">
                <p className="mb-2 font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Trust facts
                </p>
                <RailFactList facts={trustFacts} />
              </div>
            )}
            <p className="rounded-lg border border-dashed px-3 py-2 text-xs leading-relaxed text-muted-foreground">
              Approving authorizes the current safe step. Final approval may lead
              to push or PR creation in supported modes. Pipewright never merges
              automatically.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function RunMemorySuggestions({ run }: { run: Run }) {
  const navigate = useNavigate()
  const [result, setResult] =
    useState<RunMemorySuggestionGenerateResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const generateMutation = useMutation({
    mutationFn: () => memoryApi.generateRunMemorySuggestions(run.id),
    onSuccess: (data) => {
      setResult(data)
      setError(null)
    },
    onError: (mutationError: unknown) => {
      setResult(null)
      setError(
        getErrorMessage(mutationError, 'Failed to generate memory suggestions.'),
      )
    },
  })

  const hasProject = Boolean(run.project_id)
  const preview = result?.suggestions.slice(0, 5) ?? []
  const extraCount = result ? result.suggestions.length - preview.length : 0

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="text-base">Memory Suggestions</CardTitle>
        <CardDescription>
          Generate project memory suggestions from this run's outcome. Suggestions
          stay pending until you review them in Project Memory.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div>
          <Button
            size="sm"
            onClick={() => generateMutation.mutate()}
            disabled={generateMutation.isPending || !hasProject}
          >
            {generateMutation.isPending
              ? 'Generating…'
              : 'Generate memory suggestions from this run'}
          </Button>
          {!hasProject && (
            <p className="mt-2 text-xs text-muted-foreground">
              This run has no project, so no project-scoped memory can be generated.
            </p>
          )}
        </div>

        {error && (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
            {error}
          </div>
        )}

        {result && (
          <div className="grid gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">Generated {result.generated_count}</Badge>
              <Badge variant="outline">Skipped {result.skipped_count}</Badge>
              <Badge variant="outline">Blocked {result.blocked_count}</Badge>
            </div>

            {result.generated_count === 0 ? (
              <p className="text-sm text-muted-foreground">
                No new suggestions (already generated or nothing to suggest).
              </p>
            ) : (
              <ul className="grid gap-2">
                {preview.map(suggestion => (
                  <li
                    key={suggestion.id}
                    className="rounded-lg border p-3 text-sm"
                  >
                    <p className="leading-6">{suggestion.content}</p>
                    {(suggestion.source_type || suggestion.risk_level) && (
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        {suggestion.source_type && (
                          <Badge variant="outline">
                            {suggestion.source_type}
                          </Badge>
                        )}
                        {suggestion.risk_level && (
                          <Badge variant="outline">
                            risk {suggestion.risk_level}
                          </Badge>
                        )}
                      </div>
                    )}
                  </li>
                ))}
                {extraCount > 0 && (
                  <li className="text-xs text-muted-foreground">
                    +{extraCount} more
                  </li>
                )}
              </ul>
            )}

            {hasProject && (
              <div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    navigate(`/memory?projectId=${run.project_id}`)
                  }
                >
                  Review in Project Memory →
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function shouldPollRunStatus(status: RunStatus) {
  return [
    'running',
    'running_chunks',
    'paused',
    'awaiting_chunk_plan_approval',
    'awaiting_chunk_approval',
    'awaiting_final_approval',
    'awaiting_memory_conflict_approval',
    // #27F: keep polling while a scope expansion decision is pending so the run
    // status updates promptly after approve (retry) or reject.
    'awaiting_scope_approval',
    'pushing',
  ].includes(status)
}

function shouldPollChunkPlan(plan: ChunkPlanResponse) {
  if (plan.chunk_plan_status === 'awaiting_approval') return true
  if (plan.chunk_plan_status !== 'approved') return false

  return plan.chunks.some(chunk =>
    [
      'pending',
      'running',
      'failed',
      'awaiting_chunk_approval',
    ].includes(chunk.status)
  )
}

function shouldShowPushPrPanel(status: RunStatus, hasPrData: boolean) {
  return (
    hasPrData ||
    status === 'final_approved' ||
    status === 'pushing' ||
    status === 'push_failed'
  )
}

// #37D2: run statuses where the chunk plan reads as history — the current decision
// (if any) has moved above it into Finish & ship, or the run is terminal. This is
// only a coarse gate; the per-chunk attention check below is the real fail-safe
// that keeps any still-actionable chunk expanded.
const CHUNK_HISTORY_ELIGIBLE_STATUSES: RunStatus[] = [
  'complete',
  'final_approved',
  'pushing',
  'push_failed',
  'rejected',
  'final_rejected',
  'failed',
]

// #37D2: decide whether the Chunk Plan Details section should default to a
// collapsed "Chunk history" disclosure. Fail-open by construction: it returns true
// ONLY when the run is finished/terminal AND nothing in the plan needs attention.
// Any missing or ambiguous input returns false (stay expanded). It hides nothing —
// the full panel is one click away — and it never applies to a state with a
// pending chunk/plan control (those statuses are excluded, and any actionable
// chunk forces expansion via chunkNeedsAttention).
function shouldCollapseChunkHistory(
  run: Run,
  chunkPlan: ChunkPlanResponse | undefined,
): boolean {
  if (!chunkPlan) return false
  if (!CHUNK_HISTORY_ELIGIBLE_STATUSES.includes(run.status)) return false
  // Unknown state → fail open.
  if (chunkPlan.operator_state?.unknown_state_warning) return false
  // Plan-level approve/reject still pending → keep expanded.
  if (chunkPlan.chunk_plan_status === 'awaiting_approval') return false
  // Any chunk that is running / in_progress / failed / awaiting chunk approval, or
  // carries a pending scope expansion, an error, a patch failure, or a recovered
  // patch awaiting review → keep expanded. Reuses ChunkPlanPanel's own predicate
  // (activeChunkNumber = null, so only real attention signals count, not "is the
  // current chunk").
  const anyChunkNeedsAttention = chunkPlan.chunks.some(chunk =>
    chunkNeedsAttention(chunk, null),
  )
  if (anyChunkNeedsAttention) return false
  return true
}

function isPendingFinalGate(gate: ApprovalGate, runId: string) {
  return (
    gate.run_id === runId &&
    gate.approval_type === 'final' &&
    gate.chunk_number === 0 &&
    gate.status === 'pending'
  )
}

function isPendingMemoryConflictGate(gate: ApprovalGate, runId: string) {
  return (
    gate.run_id === runId &&
    gate.approval_type === 'memory_conflict' &&
    gate.chunk_number === 0 &&
    gate.status === 'pending'
  )
}

function isPendingChunkGate(gate: ApprovalGate, runId: string) {
  return (
    gate.run_id === runId &&
    gate.approval_type === 'chunk' &&
    typeof gate.chunk_number === 'number' &&
    gate.chunk_number > 0 &&
    gate.status === 'pending'
  )
}

// #35H: vertical stepper for the Finish & ship flow. Display-only — it adds no
// controls and changes no behavior. It only frames the existing final-approval →
// push/PR → checks panels as one guided, ordered flow with done/current/pending
// states. The step status is derived purely from the same booleans that already
// gate each panel, so it never diverges from what is actually shown.
type FinishStepStatus = 'done' | 'current' | 'pending'

function FinishStepBadge({ status }: { status: FinishStepStatus }) {
  const meta =
    status === 'done'
      ? { label: 'Done', className: 'border-emerald-200 bg-emerald-50 text-emerald-700' }
      : status === 'current'
        ? { label: 'Current', className: 'border-foreground/30 bg-background text-foreground' }
        : { label: 'Pending', className: 'border-muted-foreground/30 bg-muted text-muted-foreground' }
  return (
    <span
      className={`rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${meta.className}`}
    >
      {meta.label}
    </span>
  )
}

function FinishStep({
  n,
  title,
  status,
  effect,
  isLast,
  children,
}: {
  n: number
  title: string
  status: FinishStepStatus
  effect: string
  isLast?: boolean
  children: ReactNode
}) {
  const circle =
    status === 'done'
      ? 'border border-emerald-500 bg-emerald-500 text-white'
      : status === 'current'
        ? 'border-2 border-foreground bg-background font-semibold text-foreground'
        : 'border border-dashed border-muted-foreground/40 bg-muted text-muted-foreground'
  return (
    <div className="flex gap-3">
      {/* Left rail: numbered circle + connector that threads the steps together. */}
      <div className="flex flex-col items-center">
        <div
          className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full font-mono text-xs ${circle}`}
        >
          {status === 'done' ? (
            <svg
              className="h-3.5 w-3.5"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="3"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <polyline points="20 6 9 17 4 12" />
            </svg>
          ) : (
            n
          )}
        </div>
        {!isLast && <div className="mt-1 w-px flex-1 bg-border" />}
      </div>
      {/* Right column: step heading + effect copy + the existing panel/placeholder. */}
      <div className={`min-w-0 flex-1 ${isLast ? '' : 'pb-6'}`}>
        <div className="flex flex-wrap items-center gap-2">
          <h4
            className={`text-sm font-semibold ${status === 'pending' ? 'text-muted-foreground' : ''}`}
          >
            {title}
          </h4>
          <FinishStepBadge status={status} />
        </div>
        <p className="mt-0.5 text-xs text-muted-foreground">{effect}</p>
        <div className="mt-3">{children}</div>
      </div>
    </div>
  )
}

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { events, status: wsStatus } = useRunEvents(runId)
  const [viewMode, setViewMode] = useState<RunViewMode>(readStoredRunViewMode)
  const [chunkPlanActionError, setChunkPlanActionError] = useState<string | null>(
    null
  )
  const [chunkExecutionMessage, setChunkExecutionMessage] = useState<
    string | null
  >(null)
  const [chunkExecutionError, setChunkExecutionError] = useState<string | null>(
    null
  )
  const [startContextDrift, setStartContextDrift] =
    useState<StartContextDriftedResponse | null>(null)
  const [chunkActionMessage, setChunkActionMessage] = useState<string | null>(
    null
  )
  const [chunkActionError, setChunkActionError] = useState<string | null>(null)
  const [finalApprovalMessage, setFinalApprovalMessage] = useState<
    string | null
  >(null)
  const [finalApprovalError, setFinalApprovalError] = useState<string | null>(
    null
  )
  const [memoryConflictMessage, setMemoryConflictMessage] = useState<
    string | null
  >(null)
  const [memoryConflictError, setMemoryConflictError] = useState<string | null>(
    null
  )
  const [pushPrMessage, setPushPrMessage] = useState<string | null>(null)
  const [pushPrError, setPushPrError] = useState<string | null>(null)
  const [startImplementationError, setStartImplementationError] = useState<
    string | null
  >(null)

  const { data: run } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => runsApi.get(runId!),
    enabled: !!runId,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      return shouldPollRunStatus(data.status)
        ? 2000
        : false
    },
  })

  const {
    data: timelineEntries,
    isLoading: timelineLoading,
    error: timelineError,
  } = useRunTimeline(runId, {
    refetchActive: run ? shouldPollRunStatus(run.status) : false,
  })

  const { data: gates, isLoading: gatesLoading } = useQuery({
    queryKey: ['gates'],
    queryFn: gatesApi.list,
    refetchInterval: run?.status === 'paused' ? 2000 : false,
  })

  const { data: chunkPlan } = useQuery({
    queryKey: ['runChunks', runId],
    queryFn: () => runsApi.getRunChunks(runId!),
    enabled: !!runId,
    retry: false,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      return shouldPollChunkPlan(data) ? 2000 : false
    },
  })

  // Slice C: read-only plan-version lineage. Eager once a chunk plan exists (the
  // lineage describes that plan), but it adds NO polling interval — it only
  // changes on revise/approve, both of which invalidate this key below. retry:
  // false mirrors the chunk-plan query so an unknown-run 404 does not retry-storm.
  const { data: planVersions } = useQuery({
    queryKey: ['planVersions', runId],
    queryFn: () => runsApi.getPlanVersions(runId!),
    enabled: !!runId && !!chunkPlan,
    retry: false,
  })

  const refreshRunTimeline = () => {
    queryClient.invalidateQueries({ queryKey: ['runTimeline', runId] })
  }

  const refreshRunDecisionState = () => {
    queryClient.invalidateQueries({ queryKey: ['run', runId] })
    queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
    queryClient.invalidateQueries({ queryKey: ['gates'] })
    queryClient.invalidateQueries({ queryKey: ['planVersions', runId] })
    refreshRunTimeline()
  }

  const { data: project } = useQuery({
    queryKey: ['project', run?.project_id],
    queryFn: () => projectsApi.get(run!.project_id!),
    enabled: !!run?.project_id,
  })

  const pendingFinalGate = gates?.find(
    (gate: ApprovalGate) => runId ? isPendingFinalGate(gate, runId) : false
  )

  const pendingMemoryConflictGate = gates?.find(
    (gate: ApprovalGate) =>
      runId ? isPendingMemoryConflictGate(gate, runId) : false
  )

  const pendingChunkGates =
    gates?.filter((gate: ApprovalGate) =>
      runId ? isPendingChunkGate(gate, runId) : false
    ) ?? []

  const gateBackedApprovalChunkNumbers = pendingChunkGates
    .map(gate => gate.chunk_number)
    .filter((chunkNumber): chunkNumber is number =>
      typeof chunkNumber === 'number'
    )

  const pendingGate = gates?.find(
    (g: ApprovalGate) =>
      g.run_id === runId &&
      g.status === 'pending' &&
      !isPendingFinalGate(g, runId!) &&
      !isPendingMemoryConflictGate(g, runId!)
  )

  // Runtime verdict for the chunk this gate belongs to, used to keep the Human
  // Approval card from claiming "Tests have passed" when validation was weak.
  const pendingGateVerdict: TestRunVerdict | null =
    pendingGate?.chunk_number != null
      ? chunkPlan?.chunks.find(
          chunk => chunk.chunk_number === pendingGate.chunk_number
        )?.test_validation?.verdict ?? null
      : null

  const approveMutation = useMutation({
    mutationFn: () => {
      if (!pendingGate) {
        throw new Error('No pending approval gate found.')
      }
      if (runId && isPendingChunkGate(pendingGate, runId)) {
        return runsApi.approveChunk(runId, pendingGate.chunk_number!)
      }
      return gatesApi.approve(pendingGate.id)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      refreshRunTimeline()
    },
  })

  const rejectMutation = useMutation({
    mutationFn: () => {
      if (!pendingGate) {
        throw new Error('No pending approval gate found.')
      }
      if (runId && isPendingChunkGate(pendingGate, runId)) {
        return runsApi.rejectChunk(
          runId,
          pendingGate.chunk_number!,
          'Rejected by user'
        )
      }
      return gatesApi.reject(pendingGate.id, 'Rejected by user')
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      refreshRunTimeline()
    },
  })

  const approveChunkPlanMutation = useMutation({
    mutationFn: () => runsApi.approveChunkPlan(runId!),
    onSuccess: () => {
      setChunkPlanActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      setStartContextDrift(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      // Slice C: refetch lineage so the freshly stamped approved_version (Slice B)
      // surfaces its "Approved: vN" badge without a manual refresh.
      queryClient.invalidateQueries({ queryKey: ['planVersions', runId] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkPlanActionError(
        getErrorMessage(error, 'Failed to approve chunk plan.')
      )
    },
  })

  const rejectChunkPlanMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectChunkPlan(runId!, reason || 'Chunk plan rejected by user'),
    onSuccess: () => {
      setChunkPlanActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      setStartContextDrift(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkPlanActionError(
        getErrorMessage(error, 'Failed to reject chunk plan.')
      )
    },
  })

  const executeChunksMutation = useMutation({
    mutationFn: () => runsApi.executeChunks(runId!),
    onSuccess: (response) => {
      setChunkExecutionMessage(
        response.message || 'Chunk execution started.'
      )
      setChunkExecutionError(null)
      setStartContextDrift(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkExecutionMessage(null)
      const data = getResponseData(error)
      if (isStartContextDriftedResponse(data)) {
        setChunkExecutionError(null)
        setStartContextDrift(data)
        return
      }
      setStartContextDrift(null)
      setChunkExecutionError(getErrorMessage(error, 'Failed to execute chunks.'))
    },
  })

  const resumeChunksMutation = useMutation({
    mutationFn: () => runsApi.resumeChunks(runId!),
    onSuccess: (response) => {
      setChunkExecutionMessage(response.message || 'Chunked run resumed.')
      setChunkExecutionError(null)
      setStartContextDrift(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkExecutionMessage(null)
      setStartContextDrift(null)
      setChunkExecutionError(getErrorMessage(error, 'Failed to resume run.'))
    },
  })

  const approveChunkMutation = useMutation({
    mutationFn: (chunkNumber: number) => runsApi.approveChunk(runId!, chunkNumber),
    onSuccess: (response, chunkNumber) => {
      setChunkActionMessage(
        response.message || `Chunk ${chunkNumber} approved.`
      )
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getErrorMessage(error, 'Failed to approve chunk.'))
    },
  })

  const rejectChunkMutation = useMutation({
    mutationFn: ({
      chunkNumber,
      reason,
    }: {
      chunkNumber: number
      reason: string
    }) =>
      runsApi.rejectChunk(
        runId!,
        chunkNumber,
        reason || 'Chunk rejected by user'
      ),
    onSuccess: (response, variables) => {
      setChunkActionMessage(
        response.message || `Chunk ${variables.chunkNumber} rejected.`
      )
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getErrorMessage(error, 'Failed to reject chunk.'))
    },
  })

  const retryChunkMutation = useMutation({
    mutationFn: ({
      chunkNumber,
      failureReportId,
    }: {
      chunkNumber: number
      failureReportId: string
    }) => runsApi.retryChunk(runId!, chunkNumber, failureReportId),
    onSuccess: (response) => {
      setChunkActionMessage(retrySuccessMessage(response.status))
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getRetryErrorMessage(error))
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      // Refresh even on failure: the backend may have advanced the
      // failure_report_id (re-failure), so a stale id self-corrects.
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
  })

  const steerReviewFindingMutation = useMutation({
    mutationFn: ({
      chunkNumber,
      steerText,
      failureReportId,
    }: {
      chunkNumber: number
      steerText: string
      failureReportId?: string | null
    }) =>
      runsApi.steerChunk(runId!, chunkNumber, steerText, failureReportId),
    onSuccess: (response) => {
      setChunkActionMessage(retrySuccessMessage(response.status))
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      refreshRunDecisionState()
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getRetryErrorMessage(error))
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      refreshRunDecisionState()
    },
  })

  const approveFinalApprovalMutation = useMutation({
    mutationFn: () => runsApi.approveFinalApproval(runId!),
    onSuccess: () => {
      setFinalApprovalMessage('Final approval accepted.')
      setFinalApprovalError(null)
      setPushPrMessage(null)
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setFinalApprovalMessage(null)
      setFinalApprovalError(
        getErrorMessage(error, 'Failed to approve final approval.')
      )
    },
  })

  const rejectFinalApprovalMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectFinalApproval(
        runId!,
        reason || 'Final approval rejected by user'
      ),
    onSuccess: () => {
      setFinalApprovalMessage('Final approval rejected.')
      setFinalApprovalError(null)
      setPushPrMessage(null)
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setFinalApprovalMessage(null)
      setFinalApprovalError(
        getErrorMessage(error, 'Failed to reject final approval.')
      )
    },
  })

  const approveMemoryConflictMutation = useMutation({
    mutationFn: () => runsApi.approveMemoryConflict(runId!),
    onSuccess: () => {
      setMemoryConflictMessage(
        'Conflict overridden for this run. Continuing execution...'
      )
      setMemoryConflictError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
      // Override-once: the run returned to an executable state. Re-trigger
      // execution so the now-honored override lets the run continue.
      executeChunksMutation.mutate()
    },
    onError: (error: unknown) => {
      setMemoryConflictMessage(null)
      setMemoryConflictError(
        getErrorMessage(error, 'Failed to override memory conflict.')
      )
    },
  })

  const rejectMemoryConflictMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectMemoryConflict(
        runId!,
        reason || 'Memory conflict rejected by user'
      ),
    onSuccess: () => {
      setMemoryConflictMessage('Run rejected due to memory conflict.')
      setMemoryConflictError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setMemoryConflictMessage(null)
      setMemoryConflictError(
        getErrorMessage(error, 'Failed to reject memory conflict.')
      )
    },
  })

  const pushPrMutation = useMutation({
    mutationFn: () => runsApi.pushPr(runId!),
    onSuccess: (response) => {
      setPushPrMessage(
        response.pr_url
          ? `Pull request created: ${response.pr_url}`
          : 'Push/PR operation completed.'
      )
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      refreshRunTimeline()
    },
    onError: (error: unknown) => {
      setPushPrMessage(null)
      setPushPrError(getErrorMessage(error, 'Failed to push/create PR.'))
    },
  })

  const startImplementationMutation = useMutation({
    mutationFn: () => runsApi.startImplementation(runId!),
    onSuccess: (response) => {
      if (isNoRunResponse(response)) {
        setStartImplementationError(noRunHandoffMessage(response))
        return
      }
      if (!response.run_id) {
        setStartImplementationError('Implementation run was not created.')
        return
      }
      setStartImplementationError(null)
      navigate(`/runs/${response.run_id}`)
    },
    onError: (error: unknown) => {
      const data = getResponseData(error)
      if (isNoRunResponse(data)) {
        setStartImplementationError(noRunHandoffMessage(data))
        return
      }
      setStartImplementationError(
        getErrorMessage(error, 'Failed to start implementation.'),
      )
    },
  })

  if (!run) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading run...</p>
      </div>
    )
  }

  if (run.intent === 'report_only' || run.status === 'report_ready') {
    return <ReportView run={run} onBack={() => navigate(-1)} />
  }

  if (run.intent === 'plan_only' || run.status === 'plan_ready') {
    return (
      <PlanView
        run={run}
        chunkPlan={chunkPlan}
        onBack={() => navigate(-1)}
        onStartImplementation={() => startImplementationMutation.mutate()}
        isStartingImplementation={startImplementationMutation.isPending}
        startImplementationError={startImplementationError}
      />
    )
  }

  const hasPrData = Boolean(run.pr_url || run.pr_number || run.push_error)
  const showMemoryConflictPanel =
    run.status === 'awaiting_memory_conflict_approval'
  const showFinalApprovalPanel = run.status === 'awaiting_final_approval'
  const showPushPrPanel = shouldShowPushPrPanel(run.status, hasPrData)
  // #31F: the display-only PR status + checks panel shares visibility with the
  // push section. It never gates the push/approval controls; it only surfaces
  // the typed PR state and, on explicit refresh, GitHub checks.
  const showPrStatusPanel = showPushPrPanel
  // #35H: derive Finish & ship step states purely from the booleans that already
  // gate each panel, so the stepper never claims a state different from what is
  // shown. Display-only; changes no behavior.
  const hasPr = Boolean(run.pr_url)
  const donePrOpen = run.status === 'complete' && hasPr
  const showPrStatusPanelInFinish = showPrStatusPanel && !donePrOpen
  // local_only never pushes or opens a PR from inside Pipewright. When no real PR
  // exists, Finish & ship must show manual/out-of-app guidance instead of the
  // GitHub push/PR panels, so the stepper does not imply an in-app push is queued.
  // A real PR (pr_url) or a push failure still falls through to the normal panels
  // (defensive — not expected in local_only).
  const isLocalOnly = project?.pr_mode === 'local_only'
  const localOnlyManualShip =
    isLocalOnly && showPushPrPanel && !hasPr && !run.push_error
  // #39B: local_only never persists branch_name (the push/PR path that would set it
  // is never invoked from the UI), but the run branch is created deterministically at
  // execution start as `pipewright/<run-id prefix>` — the exact name the backend uses
  // everywhere (chunked_orchestrator). Prefer a persisted name if one ever exists;
  // otherwise derive the same deterministic name for display only. This creates,
  // pushes, or renames nothing — it only labels what already exists locally.
  const localRunBranch = run.branch_name || `pipewright/${run.id.slice(0, 8)}`
  // The project's configured PR base, if any. Used only to offer an accurate
  // `gh pr create` command; when unknown it is omitted so we never guess a base.
  const configuredPrBase = project?.github_base_branch?.trim() || null
  // Copy-pasteable manual ship commands for local_only. The `gh pr create` line is
  // included only when the base branch is known; otherwise the user is told in plain
  // copy to open the PR manually. Display-only — Pipewright runs none of these.
  const localShipCommands = configuredPrBase
    ? `git checkout ${localRunBranch}\n` +
      `git push -u origin ${localRunBranch}\n` +
      `gh pr create --base ${configuredPrBase} --head ${localRunBranch}`
    : `git checkout ${localRunBranch}\n` +
      `git push -u origin ${localRunBranch}`
  const isDeveloperMode = viewMode === 'developer'
  const setRunViewMode = (nextMode: RunViewMode) => {
    setViewMode(nextMode)
    writeStoredRunViewMode(nextMode)
  }
  const finishStep1Status: FinishStepStatus = showFinalApprovalPanel
    ? 'current'
    : 'done'
  const finishStep2Status: FinishStepStatus = !showPushPrPanel
    ? 'pending'
    : hasPr
      ? 'done'
      : 'current'
  // local_only has no PR-checks step, so step 3 stays pending (nothing to do)
  // rather than "current".
  const finishStep3Status: FinishStepStatus = localOnlyManualShip
    ? 'pending'
    : donePrOpen
      ? 'done'
      : showPrStatusPanelInFinish
      ? 'current'
      : 'pending'
  const finishStep2Title = localOnlyManualShip ? 'Finish locally' : 'Push / create PR'
  const finishStep2Effect = localOnlyManualShip
    ? 'Local-only: changes stay on your machine — push manually outside Pipewright.'
    : 'Push or create the pull request — this never merges.'
  const finishStep3Effect = localOnlyManualShip
    ? 'Local-only mode creates no pull request, so there is nothing to refresh.'
    : 'Checks refresh only when you ask — nothing polls automatically.'
  // #28G: pre-disable Approve Final when any chunk's weak/none verdict is not
  // acknowledged against the current diff. The backend #28F gate stays the
  // source of truth; this only avoids sending the user into an avoidable 409.
  const acknowledgementBlocking = Boolean(
    chunkPlan?.chunks?.some(chunk => {
      const status = chunk.test_validation?.acknowledgement_status
      return (
        chunk.test_validation?.requires_acknowledgement === true &&
        (status === 'missing' || status === 'stale')
      )
    }),
  )
  const reviewAcknowledgementBlocking = Boolean(
    chunkPlan?.chunks?.some(chunk => {
      const status = chunk.review?.acknowledgement_status
      return (
        chunk.review?.requires_acknowledgement === true &&
        (status === 'missing' || status === 'stale')
      )
    }),
  )

  // #35F: map an operator_state primary_action to the SAME legacy mutation its
  // twin control already calls. Returns null for unmapped IDs, or when the
  // equivalent legacy control would itself be unavailable/disabled, so the panel
  // falls back to its existing display-only preview. This adds reachability from
  // the top panel only — no new routes and no changed semantics.
  const resolvePrimaryAction = (
    action: OperatorAction,
  ): { onClick: () => void; isPending: boolean } | null => {
    const key = primaryActionHandlerKey(action.id)
    if (!key) return null
    switch (key) {
      case 'approve_plan':
        return {
          onClick: () => approveChunkPlanMutation.mutate(),
          isPending: approveChunkPlanMutation.isPending,
        }
      case 'execute_chunks':
        return {
          onClick: () => executeChunksMutation.mutate(),
          isPending: executeChunksMutation.isPending,
        }
      case 'approve_final':
        // Mirror FinalApprovalPanel's enable rule so the top button is never
        // clickable when the legacy Approve Final would be hidden/disabled.
        if (
          !pendingFinalGate ||
          acknowledgementBlocking ||
          reviewAcknowledgementBlocking
        ) return null
        return {
          onClick: () => approveFinalApprovalMutation.mutate(),
          isPending: approveFinalApprovalMutation.isPending,
        }
      case 'create_pr': {
        // Mirror PushPrPanel's in-app push affordance. The legacy button renders
        // on canPush (run-field based, line ~43 of PushPrPanel). We additionally
        // require a PR mode that actually pushes / creates a PR inside the app;
        // in local_only mode the panel only shows manual/out-of-app guidance, so
        // the top action must stay display-only there.
        const canPush =
          (run.status === 'final_approved' || run.status === 'push_failed') &&
          !run.pr_url
        const prModeSupportsInAppPush =
          project?.pr_mode === 'github_cli' ||
          project?.pr_mode === 'manual_token'
        if (!canPush || !prModeSupportsInAppPush) return null
        return {
          onClick: () => pushPrMutation.mutate(),
          isPending: pushPrMutation.isPending,
        }
      }
      default:
        return null
    }
  }

  // #35G: map operator_state neutral/secondary (co-equal) actions to the SAME
  // legacy mutation their twin control already calls. Only the memory-conflict
  // pair is mapped, and only while the legacy MemoryConflictPanel would show its
  // buttons (a pending memory-conflict gate exists and gates are loaded). Scope
  // expansion and weak-test acknowledgement stay display-only (see helper). No
  // new routes, no changed semantics — only reachability from the top panel.
  const resolveCoEqualAction = (
    action: OperatorAction,
  ): { onClick: () => void; isPending: boolean } | null => {
    const key = coEqualActionHandlerKey(action.id)
    if (!key) return null
    // Mirror MemoryConflictPanel's enable rule (actionPending / hasPendingGate).
    if (!pendingMemoryConflictGate || gatesLoading) return null
    switch (key) {
      case 'approve_memory_conflict':
        return {
          onClick: () => approveMemoryConflictMutation.mutate(),
          isPending: approveMemoryConflictMutation.isPending,
        }
      case 'reject_memory_conflict':
        // Reason is optional in the legacy panel; '' uses the same backend
        // default the empty-textarea reject already sends. The panel below stays
        // available for users who want to add a reason.
        return {
          onClick: () => rejectMemoryConflictMutation.mutate(''),
          isPending: rejectMemoryConflictMutation.isPending,
        }
      default:
        return null
    }
  }

  const chunkSummary = chunkSummaryText(run)
  const operatorState = chunkPlan?.operator_state ?? null
  const contextRailMode = getRunContextRailMode(run, operatorState)
  // #37D2: in finished/terminal states with no pending chunk action, the chunk
  // plan reads as history and may collapse into a disclosure (fail-open helper).
  const collapseChunkHistory = shouldCollapseChunkHistory(run, chunkPlan)
  // #26E2 retry-affordance parity: the PatchFailureBanner's enabled "Retry code
  // change" button must reflect the authoritative human-retry eligibility, not
  // just the recovery-suggestion vocabulary. operator_state offers `retry_patch`
  // as the PROGRESS primary_action only when evaluate_patch_retry_eligibility()
  // allows a human retry and no scope/branch/status condition blocks it; when
  // retry is ineligible (e.g. NO_CHANGES → disallowed_failure_type) it appears in
  // blocked_actions instead. Mirror that single source of truth here. Display-only:
  // the backend route stays the enforcing gate.
  const retryEligible = operatorState?.primary_action?.id === 'retry_patch'
  const retryBlockedReason =
    operatorState?.blocked_actions?.find(
      action => action.id === 'retry_patch',
    )?.blocked_reason ?? null
  // #39A: hide a lower legacy PRIMARY button only when the SAME action is wired and
  // clickable in the top OperatorAttentionPanel. We reuse resolvePrimaryAction (the
  // single source of truth for which primary_action ids are clickable up top) so the
  // two surfaces can never disagree. Fail-open: a missing operator_state, a
  // different/blocked action id, a risk_decision, or a null resolver all leave the
  // legacy button visible. Composition/display only — no mutation or wiring change,
  // and Reject Plan / Resume Run are never hidden (they have no top twin).
  const operatorPrimaryAction = operatorState?.primary_action ?? null
  const topPrimaryIsRisk = operatorState?.decision_type === 'risk_decision'
  const topPrimaryWired =
    operatorPrimaryAction && !topPrimaryIsRisk
      ? resolvePrimaryAction(operatorPrimaryAction)
      : null
  const hideLegacyPlanApprove =
    operatorPrimaryAction?.id === 'approve_plan' && topPrimaryWired !== null
  const hideLegacyExecute =
    operatorPrimaryAction?.id === 'execute_chunks' && topPrimaryWired !== null
  // The chunk plan panel (or the legacy empty-state) is built once here and placed
  // either inline or inside the history disclosure below — identical either way,
  // so no control, banner, prop, or handler changes.
  const chunkPlanContent = chunkPlan ? (
    <ChunkPlanPanel
      plan={chunkPlan}
      isApproving={approveChunkPlanMutation.isPending}
      isRejecting={rejectChunkPlanMutation.isPending}
      isExecuting={executeChunksMutation.isPending}
      isResuming={resumeChunksMutation.isPending}
      approvingChunkNumber={approveChunkMutation.variables ?? null}
      rejectingChunkNumber={
        rejectChunkMutation.variables?.chunkNumber ?? null
      }
      error={chunkPlanActionError}
      executionMessage={chunkExecutionMessage}
      executionError={chunkExecutionError}
      startContextDrift={startContextDrift}
      chunkActionMessage={chunkActionMessage}
      chunkActionError={chunkActionError}
      planVersions={planVersions}
      hiddenApprovalChunkNumbers={gateBackedApprovalChunkNumbers}
      retryingChunkNumber={
        retryChunkMutation.isPending
          ? retryChunkMutation.variables?.chunkNumber ?? null
          : null
      }
      steeringChunkNumber={
        steerReviewFindingMutation.isPending
          ? steerReviewFindingMutation.variables?.chunkNumber ?? null
          : null
      }
      retryEligible={retryEligible}
      retryBlockedReason={retryBlockedReason}
      hideLegacyPlanApprove={hideLegacyPlanApprove}
      hideLegacyExecute={hideLegacyExecute}
      onApprove={() => approveChunkPlanMutation.mutate()}
      onReject={(reason) => rejectChunkPlanMutation.mutate(reason)}
      onExecute={() => executeChunksMutation.mutate()}
      onResume={() => resumeChunksMutation.mutate()}
      onApproveChunk={(chunkNumber) =>
        approveChunkMutation.mutate(chunkNumber)
      }
      onRejectChunk={(chunkNumber, reason) =>
        rejectChunkMutation.mutate({ chunkNumber, reason })
      }
      onRetryChunk={(chunkNumber, failureReportId) =>
        retryChunkMutation.mutate({ chunkNumber, failureReportId })
      }
      onSteerReviewFinding={(chunkNumber, steerText, failureReportId) =>
        steerReviewFindingMutation.mutate({
          chunkNumber,
          steerText,
          failureReportId,
        })
      }
      onReviewActionComplete={refreshRunDecisionState}
      onPlanRevised={refreshRunDecisionState}
      onScopeActionComplete={() => {
        // #27F: refresh run/chunks/gates after a scope expansion
        // approve/reject, matching the invalidation used by the chunk
        // approve/reject/retry mutations above.
        refreshRunDecisionState()
      }}
    />
  ) : (
    <Card className="mb-4 border-dashed">
      <CardContent className="py-6">
        <p className="text-sm font-medium">No chunk plan loaded</p>
        <p className="mt-1 text-sm text-muted-foreground">
          This run started before chunked plans. Showing what we have.
        </p>
      </CardContent>
    </Card>
  )

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Pipeline Run
          </p>
          <h2 className="text-2xl font-bold leading-tight mt-1 line-clamp-2">
            {run.feature_description || 'Untitled run'}
          </h2>
          {/* #37A: header meta row. The run id stays; total/current chunk moved
              here from the removed Run Summary card so the page no longer
              duplicates that information lower down. */}
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-xs text-muted-foreground">
            <span>
              run <span title={run.id}>{run.id.slice(0, 8)}</span>
            </span>
            {chunkSummary && (
              <>
                <span aria-hidden="true">·</span>
                <span>{chunkSummary}</span>
              </>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <RunStatusBadge status={run.status} friendly />
          <div
            className="inline-flex rounded-lg border bg-background p-1"
            role="group"
            aria-label="Run detail view mode"
          >
            <Button
              type="button"
              size="sm"
              variant={viewMode === 'plain' ? 'default' : 'ghost'}
              onClick={() => setRunViewMode('plain')}
              className="h-7 px-2 text-xs"
            >
              Plain English
            </Button>
            <Button
              type="button"
              size="sm"
              variant={isDeveloperMode ? 'default' : 'ghost'}
              onClick={() => setRunViewMode('developer')}
              className="h-7 px-2 text-xs"
            >
              Developer
            </Button>
          </div>
        </div>
      </div>

      {/* #37A: calm pipeline rail replaces the old Run Summary StepIndicator.
          Display-only — it mirrors run.current_step / run.status and changes no
          behavior, data, or wiring. */}
      <PipelineRail currentStep={run.current_step} status={run.status} />

      {/* #35E: compact read-only safety overview. Summarizes existing signals;
          the detailed banners/cards below remain the source of truth. */}
      <RunSafetyStrip run={run} chunkPlan={chunkPlan} project={project} />

      {/* Phase 2G: cockpit + context rail shell. The rail is read-only context;
          all actions stay inside the existing cockpit/action components. */}
      {(operatorState || contextRailMode === 'pr') && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)] lg:items-start xl:gap-6">
          <div className="min-w-0">
            {/* Phase 2F PR-3: promote the existing next-action engine into a sticky
                banner when the run is waiting on a human. The same resolvers are
                passed through; this changes placement/prominence, not behavior. */}
            {operatorState && (
              <section
                className={`${
                  operatorState.waiting_on === 'human'
                    ? // Cap the sticky banner to the viewport and let it scroll
                      // internally so a tall panel cannot cover the page content
                      // below it on short screens (top-3 = 0.75rem top inset).
                      'sticky top-3 z-30 max-h-[calc(100vh-1.5rem)] overflow-y-auto rounded-xl bg-background/95 pb-1 shadow-sm backdrop-blur'
                    : ''
                }`}
              >
                <OperatorAttentionPanel
                  operatorState={operatorState}
                  resolvePrimaryAction={resolvePrimaryAction}
                  resolveCoEqualAction={resolveCoEqualAction}
                />
              </section>
            )}
          </div>
          <aside
            className="min-w-0"
            data-run-context-rail={contextRailMode ?? 'empty'}
            aria-hidden={contextRailMode ? undefined : true}
          >
            <RunContextRail
              run={run}
              operatorState={operatorState}
              project={project}
            />
          </aside>
        </div>
      )}

      <section className="mb-6">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold">Run timeline</h3>
            <p className="text-xs text-muted-foreground">
              The readable spine of this run: persisted milestones with the live
              tail merged in.
            </p>
          </div>
          <span className="rounded-full border bg-background px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            Read-only
          </span>
        </div>
        <Card className="bg-[var(--pw-bg-elev)]">
          <CardContent className="py-4">
            <RunTimeline
              entries={timelineEntries}
              liveEvents={events}
              isLoading={timelineLoading}
              error={timelineError}
              viewMode={viewMode}
            />
          </CardContent>
        </Card>
      </section>

      {/* #35H: in final-stage states the Finish & ship flow is the current
          decision context (weak-test ack, final approval, push/PR, checks), so
          it renders here — directly after the guided panel and ABOVE Chunk Plan
          Details. The chunk plan stays rendered and functional below. This is
          ordering + composition only; every panel keeps its existing component,
          props, conditions, mutation handlers, and loading/error states. Nothing
          auto-pushes, auto-merges, or auto-refreshes checks.
          showPrStatusPanel currently equals showPushPrPanel; it is listed in the
          gate explicitly so PR status/checks visibility is guaranteed even if
          that definition ever diverges. */}
      {(showFinalApprovalPanel || showPushPrPanel || showPrStatusPanel) && (
        <Card className="mb-6 bg-muted/20">
          <CardHeader>
            <CardTitle className="text-base">Finish &amp; ship</CardTitle>
            <CardDescription>
              Final approval, push, and PR checks happen in order. Pipewright
              never merges automatically.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <TestCommandQualityWarning project={project} context="review" />

            <div>
              <FinishStep
                n={1}
                title="Final approval"
                status={finishStep1Status}
                effect="Final approval authorizes finishing the run."
              >
                {showFinalApprovalPanel ? (
                  <div className="grid gap-4">
                    {chunkPlan && (
                      <TestValidationAckPanel
                        runId={runId!}
                        plan={chunkPlan}
                        onAcknowledged={() => {
                          // Refresh run/chunks/gates so acknowledgement_status
                          // and the Approve-Final disabled state recompute,
                          // matching the invalidation used by the other
                          // mutations here.
                          queryClient.invalidateQueries({
                            queryKey: ['run', runId],
                          })
                          queryClient.invalidateQueries({
                            queryKey: ['runChunks', runId],
                          })
                          queryClient.invalidateQueries({ queryKey: ['gates'] })
                          refreshRunTimeline()
                        }}
                      />
                    )}
                    {chunkPlan && (
                      <ReviewFindingsAckPanel
                        runId={runId!}
                        chunks={chunkPlan.chunks}
                        onAcknowledged={refreshRunDecisionState}
                      />
                    )}
                    <FinalApprovalPanel
                      run={run}
                      hasPendingFinalGate={Boolean(pendingFinalGate)}
                      isCheckingFinalGate={gatesLoading}
                      isApproving={approveFinalApprovalMutation.isPending}
                      isRejecting={rejectFinalApprovalMutation.isPending}
                      message={finalApprovalMessage}
                      error={finalApprovalError}
                      acknowledgementBlocking={acknowledgementBlocking}
                      reviewAcknowledgementBlocking={reviewAcknowledgementBlocking}
                      onApprove={() => approveFinalApprovalMutation.mutate()}
                      onReject={(reason) =>
                        rejectFinalApprovalMutation.mutate(reason)
                      }
                    />
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Final approval is complete.
                  </p>
                )}
              </FinishStep>

              <FinishStep
                n={2}
                title={finishStep2Title}
                status={finishStep2Status}
                effect={finishStep2Effect}
              >
                {donePrOpen ? (
                  <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground">
                    Pull request opened. See the PR panel for identity and
                    checks.
                  </p>
                ) : localOnlyManualShip ? (
                  // local_only: no in-app push/PR. Show manual out-of-app
                  // guidance instead of the GitHub push panel so this never
                  // looks like Pipewright can open a PR for you.
                  <div className="grid gap-3 rounded-lg border border-dashed px-3 py-3 text-sm">
                    <div>
                      <p className="font-medium text-foreground">
                        Completed locally
                      </p>
                      <p className="mt-1 text-muted-foreground">
                        Pipewright created a local commit on the run branch. This
                        project is in local_only mode, so Pipewright will not push
                        or create a pull request — and it never merges
                        automatically.
                      </p>
                    </div>

                    <dl className="grid gap-2 sm:grid-cols-3">
                      <div>
                        <dt className="font-medium text-foreground">
                          Local branch
                        </dt>
                        <dd className="break-words font-mono text-xs text-muted-foreground">
                          {localRunBranch}
                        </dd>
                      </div>
                      <div>
                        <dt className="font-medium text-foreground">
                          Remote branch
                        </dt>
                        <dd className="text-muted-foreground">Not pushed</dd>
                      </div>
                      <div>
                        <dt className="font-medium text-foreground">
                          Pull request
                        </dt>
                        <dd className="text-muted-foreground">
                          Not created in local_only mode
                        </dd>
                      </div>
                    </dl>

                    <div className="grid gap-1">
                      <p className="text-xs text-muted-foreground">
                        To push and open a pull request yourself, outside
                        Pipewright:
                      </p>
                      <pre className="overflow-x-auto rounded bg-muted px-3 py-2 font-mono text-xs text-foreground">
                        {localShipCommands}
                      </pre>
                      {!configuredPrBase && (
                        <p className="text-xs text-muted-foreground">
                          Then open a pull request manually against your team’s
                          configured base branch.
                        </p>
                      )}
                    </div>
                  </div>
                ) : showPushPrPanel ? (
                  <PushPrPanel
                    run={run}
                    project={project}
                    isPushing={pushPrMutation.isPending}
                    message={pushPrMessage}
                    error={pushPrError}
                    onPush={() => pushPrMutation.mutate()}
                  />
                ) : (
                  <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground">
                    Available after final approval.
                  </p>
                )}
              </FinishStep>

              <FinishStep
                n={3}
                title="Pull request & checks"
                status={finishStep3Status}
                effect={finishStep3Effect}
                isLast
              >
                {localOnlyManualShip ? (
                  <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground">
                    No GitHub pull request is created in local-only mode, so there
                    are no PR checks to show here.
                  </p>
                ) : donePrOpen ? (
                  <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground">
                    PR identity and GitHub check details are shown in the PR
                    panel.
                  </p>
                ) : showPrStatusPanelInFinish ? (
                  <PrStatusPanel run={run} project={project} />
                ) : (
                  <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground">
                    Appears once a push or pull request exists.
                  </p>
                )}
              </FinishStep>
            </div>
          </CardContent>
        </Card>
      )}

      {TERMINAL_RUN_STATUSES.includes(run.status) && (
        <>
          <RunMemorySuggestionsDigest runId={run.id} />
          <RunMemorySuggestions run={run} />
        </>
      )}

      <section className="mb-6">
        {collapseChunkHistory ? (
          // #37D2: finished/terminal run with no pending chunk action — the chunk
          // plan is history, so demote it into a default-collapsed disclosure so it
          // no longer dominates the page. The panel inside is unchanged: every
          // control, banner, and chunk detail is one click away. This is separate
          // from the #35D "Details & audit" disclosure below — the two are not
          // merged.
          <details className="group rounded-xl border bg-card">
            <summary className="flex cursor-pointer list-none items-start gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden">
              <svg
                className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <polyline points="9 18 15 12 9 6" />
              </svg>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-semibold">Chunk history</span>
                  <span className="text-xs text-muted-foreground">
                    Implementation details from this run
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  The run is finished; full chunk details remain available below.
                  Collapsed by default — nothing was removed.
                </p>
              </div>
            </summary>
            <div className="border-t px-4 py-4">{chunkPlanContent}</div>
          </details>
        ) : (
          <>
            <div className="mb-3">
              <h3 className="text-sm font-semibold">Chunk Plan Details</h3>
              <p className="text-xs text-muted-foreground">
                The chunk-by-chunk plan, execution controls, and chunk-level
                approvals.
              </p>
            </div>
            {chunkPlanContent}
          </>
        )}
      </section>

      {pendingGate && (
        <Card className="mb-4 border-yellow-400">
          <CardHeader>
            <CardTitle className="text-base">
              Human Approval Required
            </CardTitle>
            <CardDescription>
              Review the changes and approve or reject
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">Risk:</span>
              <span
                className={`text-sm font-medium ${
                  pendingGate.risk_level === 'high'
                    ? 'text-red-500'
                    : pendingGate.risk_level === 'medium'
                    ? 'text-yellow-500'
                    : 'text-green-500'
                }`}
              >
                {pendingGate.risk_level}
              </span>
            </div>

            {pendingGate.ai_summary && (
              <div>
                <p className="text-sm font-medium mb-2">Summary</p>
                <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                  {approvalSummaryForDisplay(
                    pendingGate.ai_summary,
                    pendingGateVerdict
                  )}
                </p>
              </div>
            )}

            <Separator />

            {pendingGate.diff && (
              <div>
                <p className="text-sm font-medium mb-2">Diff</p>
                <pre className="text-xs bg-muted p-3 rounded overflow-auto max-h-64 whitespace-pre-wrap font-mono">
                  {pendingGate.diff}
                </pre>
              </div>
            )}

            <Separator />

            <div className="flex gap-3">
              <Button
                onClick={() => approveMutation.mutate()}
                disabled={approveMutation.isPending}
                className="bg-green-600 hover:bg-green-700 text-white"
              >
                {approveMutation.isPending
                  ? 'Approving...'
                  : 'Approve'}
              </Button>
              <Button
                variant="destructive"
                onClick={() => rejectMutation.mutate()}
                disabled={rejectMutation.isPending}
              >
                {rejectMutation.isPending
                  ? 'Rejecting...'
                  : 'Reject'}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {showMemoryConflictPanel && (
        <section className="mb-6">
          <div className="mb-3">
            <h3 className="text-sm font-semibold">Memory Conflict</h3>
            <p className="text-xs text-muted-foreground">
              A DB memory conflict paused this run. Override once to continue or
              reject the run.
            </p>
          </div>
          <MemoryConflictPanel
            gate={pendingMemoryConflictGate}
            isCheckingGate={gatesLoading}
            isApproving={approveMemoryConflictMutation.isPending}
            isRejecting={rejectMemoryConflictMutation.isPending}
            message={memoryConflictMessage}
            error={memoryConflictError}
            onApprove={() => approveMemoryConflictMutation.mutate()}
            onReject={(reason) => rejectMemoryConflictMutation.mutate(reason)}
          />
        </section>
      )}

      {/* #35H: Finish & ship is rendered earlier (above Chunk Plan Details) in
          final-stage states — see the section after OperatorAttentionPanel. */}

      {run.status === 'complete' && (
        <Card className="mb-4 border-green-500">
          <CardContent className="py-4">
            {run.current_step === 'local_only_complete' ? (
              <>
                <p className="text-sm font-medium text-green-600">
                  Pipeline completed locally.
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  No GitHub PR was created because this project is in Local-only
                  mode. Your changes were committed to the local Pipewright
                  branch
                  {run.branch_name ? ` (${run.branch_name})` : ''}. Push the
                  branch manually if you want to open a PR.
                </p>
              </>
            ) : run.pr_url ? (
              <>
                <p className="text-sm font-medium text-green-600">
                  Pipeline completed successfully. Pipewright never merges
                  automatically.
                </p>
              </>
            ) : (
              <p className="text-sm font-medium text-green-600">
                Pipeline completed successfully.
              </p>
            )}
            <div className="mt-3">
              <TestCommandQualityWarning project={project} context="review" />
            </div>
          </CardContent>
        </Card>
      )}

      {run.status === 'failed' && (
        <Card className="mb-4 border-red-500">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-red-600">
              This run stopped before finishing.
              {run.current_step && (
                <span className="font-normal text-muted-foreground">
                  {' '}(during: {run.current_step})
                </span>
              )}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Open Details &amp; audit below to see the timeline and full
              sequence. Nothing was pushed to GitHub and no merge was performed.
            </p>
          </CardContent>
        </Card>
      )}

      {(run.status === 'rejected' || run.status === 'final_rejected') && (
        <Card className="mb-4 border-gray-400">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-gray-600">
              This run was rejected and the changes were rolled back.
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Nothing was pushed to GitHub.
            </p>
          </CardContent>
        </Card>
      )}

      {/* #35D: the noisy diagnostic/audit sections collapse into one
          default-closed "Details & audit" area. Nothing is removed: memory and
          provider diagnostics stay available in both modes, while the raw live
          log appears in Developer mode only. */}
      <details className="group mb-6 rounded-xl border bg-card">
        <summary className="flex cursor-pointer list-none items-start gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden">
          <svg
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="9 18 15 12 9 6" />
          </svg>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-semibold">Details &amp; audit</span>
              <span className="text-xs text-muted-foreground">
                Everything technical, one click away
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {isDeveloperMode ? (
                <>
                  Live log ({events.length}{' '}
                  {events.length === 1 ? 'event' : 'events'}) · Memory used ·
                  AI setup
                </>
              ) : (
                <>Memory used · AI setup</>
              )}
              {' '}— collapsed by default, nothing was removed.
            </p>
          </div>
        </summary>

        <div className="space-y-6 border-t px-4 py-4">
          {isDeveloperMode && (
            <section>
              <div className="mb-3">
                <h3 className="text-sm font-semibold">Raw live event log</h3>
                <p className="text-xs text-muted-foreground">
                  Developer-oriented event stream. The readable timeline is shown
                  above as the primary run spine.
                </p>
              </div>
              <Card>
                <CardContent className="py-4">
                  <EventLog events={events} status={wsStatus} />
                </CardContent>
              </Card>
            </section>
          )}

          <section>
            <div className="mb-3">
              <h3 className="text-sm font-semibold">Memory Used</h3>
              <p className="text-xs text-muted-foreground">
                Display-only view of memory given to the AI during this run.
              </p>
            </div>
            <RunMemoryProvenancePanel runId={run.id} />
          </section>

          <section>
            <div className="mb-3">
              <h3 className="text-sm font-semibold">Environment</h3>
              <p className="text-xs text-muted-foreground">
                Read-only provider/model setup for each AI role.
              </p>
            </div>
            <ProviderDiagnosticsPanel />
          </section>
        </div>
      </details>

      <div className="mt-4">
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate(-1)}
        >
          Back
        </Button>
      </div>
    </div>
  )
}
