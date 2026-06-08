import { useState, type ReactNode } from 'react'
import type {
  ChunkDefinition,
  ChunkPlanResponse,
  ChunkStatus,
  StartContextDriftedResponse,
  TestRunValidation,
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
import { Separator } from '@/components/ui/separator'
import { Textarea } from '@/components/ui/textarea'
import { getStatusDisplay } from '@/utils/statusDisplay'
import PatchFailureBanner from '@/components/PatchFailureBanner'
import ScopeExpansionBanner from '@/components/ScopeExpansionBanner'
import RuntimeTestValidationBanner from '@/components/RuntimeTestValidationBanner'
import AdvisoryReviewPanel from '@/components/AdvisoryReviewPanel'
import AttemptHistory from '@/components/AttemptHistory'
import {
  parsePatchFailureSummary,
  parseRecoveredPatchReviewSummary,
  type RecoveredPatchReviewSummary,
} from '@/utils/patchFailure'
import { extractScopeWarnings } from '@/utils/scopeWarnings'

interface ChunkPlanPanelProps {
  plan: ChunkPlanResponse
  isApproving: boolean
  isRejecting: boolean
  isExecuting: boolean
  isResuming: boolean
  approvingChunkNumber: number | null
  rejectingChunkNumber: number | null
  error: string | null
  executionMessage: string | null
  executionError: string | null
  startContextDrift?: StartContextDriftedResponse | null
  chunkActionMessage: string | null
  chunkActionError: string | null
  hiddenApprovalChunkNumbers?: number[]
  // Patch retry wiring (#26E2). Optional so existing callers/tests stay valid.
  retryingChunkNumber?: number | null
  // #27F: called after a successful scope expansion approve/reject so the parent
  // refreshes run/chunks/gates query data via the existing invalidation pattern.
  onScopeActionComplete?: () => void
  onApprove: () => void
  onReject: (reason: string) => void
  onExecute: () => void
  onResume: () => void
  onApproveChunk: (chunkNumber: number) => void
  onRejectChunk: (chunkNumber: number, reason: string) => void
  onRetryChunk?: (chunkNumber: number, failureReportId: string) => void
}

function formatList(values: Array<string | number>) {
  if (values.length === 0) return 'None'
  return values.join(', ')
}

function getChunkDefinition(
  chunk: ChunkStatus,
  definitionsByNumber: Map<number, ChunkDefinition>
) {
  return definitionsByNumber.get(chunk.chunk_number)
}

// #36C: chunk statuses that mean a chunk is currently working or needs attention.
// Used only to pick the "current chunk at a glance" when the backend did not give
// an explicit current_chunk_number. 'in_progress' is included defensively even
// though the typed enum uses 'running'.
const ACTIVE_ATTENTION_STATUSES = new Set<string>([
  'running',
  'in_progress',
  'failed',
  'awaiting_chunk_approval',
])

// #36C: conservatively choose the active chunk from EXISTING data only. Prefers
// the backend's current_chunk_number; otherwise the first chunk (in plan order)
// whose status indicates work/attention. Returns null when nothing clearly
// qualifies — the caller then renders no summary card. Invents no backend state.
function selectActiveChunk(plan: ChunkPlanResponse): ChunkStatus | null {
  const chunks = plan.chunks ?? []
  if (chunks.length === 0) return null
  if (plan.current_chunk_number) {
    const current = chunks.find(
      chunk => chunk.chunk_number === plan.current_chunk_number
    )
    if (current) return current
  }
  return (
    chunks.find(chunk => ACTIVE_ATTENTION_STATUSES.has(chunk.status)) ?? null
  )
}

// #36D: whether a chunk must stay expanded by default — i.e. it is the active
// chunk or carries a pending decision / recovery / control. Pure; reads existing
// data only. Chunks where this is true render the full ChunkCard and are never
// collapsible, so a chunk that needs attention can never become invisible.
function chunkNeedsAttention(
  chunk: ChunkStatus,
  activeChunkNumber: number | null
): boolean {
  if (activeChunkNumber !== null && chunk.chunk_number === activeChunkNumber) {
    return true
  }
  // running / in_progress / failed / awaiting_chunk_approval
  if (ACTIVE_ATTENTION_STATUSES.has(chunk.status)) return true
  if (chunk.pending_scope_expansion) return true
  if (chunk.error_message) return true
  if (parsePatchFailureSummary(chunk.completion_summary)) return true
  if (parseRecoveredPatchReviewSummary(chunk.completion_summary)) return true
  return false
}

// #36C: compact, friendly summaries for the ActiveChunkCard chips. These mirror
// the verdict vocabulary already shown in full by RuntimeTestValidationBanner /
// AdvisoryReviewPanel below; they never replace those banners.
const ACTIVE_TEST_SUMMARY: Record<
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

const ACTIVE_REVIEW_SUMMARY: Record<
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

// Compact files list for the at-a-glance card; the full list stays in ChunkCard.
function truncateFiles(files: string[], max = 3): string {
  if (files.length === 0) return 'None'
  if (files.length <= max) return files.join(', ')
  return `${files.slice(0, max).join(', ')} +${files.length - max} more`
}

// Display-only marker for a recovered patch awaiting review (#26E3). The patch
// was regenerated and applied, but is NOT committed — the existing
// awaiting_chunk_approval UI below still owns approve/commit. This adds no
// approval controls; it is context only. It must not imply code correctness, and
// must not claim meaningful tests ran when runtime validation was weak/absent.
function RecoveredReviewMarker({
  summary,
  validation,
}: {
  summary: RecoveredPatchReviewSummary
  validation?: TestRunValidation | null
}) {
  // Trust the recorded runtime verdict over the summary's weak flag: only a
  // "strong" verdict may claim tests passed. Weak/none/unknown or a missing
  // verdict means meaningful validation was not confirmed.
  const strongValidation = validation?.verdict === 'strong'
  const weakTest = summary.weak_test_warning === true || !strongValidation

  return (
    <div className="grid gap-3 rounded border border-green-500 bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-green-600">
          Recovered patch ready for review
        </p>
        {weakTest && (
          <Badge
            variant="outline"
            className="border-amber-300 bg-amber-100 text-amber-800"
          >
            Weak test
          </Badge>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        {strongValidation
          ? 'Retry applied and tests passed. Review the recovered patch before committing.'
          : 'Retry applied, but meaningful test validation was not confirmed. Review the recovered patch carefully before committing.'}
      </p>

      {weakTest && (
        <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          Runtime validation for this recovered change was weak or unconfirmed.
          Review carefully before approving.
        </p>
      )}

      {summary.attempts && summary.attempts.length > 0 && (
        <AttemptHistory
          attempts={summary.attempts}
          validationVerdict={validation?.verdict}
        />
      )}
    </div>
  )
}

// Plan-level summary: totals, current chunk, complexity, and the feature
// description. Display-only; #36B extraction of the original inline summary block
// (no visual/behavior change).
function ChunkPlanSummary({ plan }: { plan: ChunkPlanResponse }) {
  const featureDescription =
    plan.triage?.feature_description || 'Feature description not available.'

  return (
    <>
      <div className="grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <p className="font-medium">Total Chunks</p>
          <p className="text-muted-foreground">{plan.total_chunks}</p>
        </div>
        <div>
          <p className="font-medium">Current Chunk</p>
          <p className="text-muted-foreground">
            {plan.current_chunk_number || 'None'}
          </p>
        </div>
        {plan.triage?.complexity && (
          <div>
            <p className="font-medium">Complexity</p>
            <p className="text-muted-foreground">
              {plan.triage.complexity}
            </p>
          </div>
        )}
      </div>

      <div>
        <p className="text-sm font-medium mb-1">Feature Description</p>
        <p className="text-sm text-muted-foreground whitespace-pre-wrap">
          {featureDescription}
        </p>
      </div>
    </>
  )
}

interface ExecutionControlsProps {
  isExecuting: boolean
  isResuming: boolean
  actionPending: boolean
  executionMessage: string | null
  executionError: string | null
  startContextDrift?: StartContextDriftedResponse | null
  onExecute: () => void
  onResume: () => void
}

// Execute / Resume controls plus the start-context-drift warning. #36B extraction
// of the original isApproved execution block (no visual/behavior change).
function ExecutionControls({
  isExecuting,
  isResuming,
  actionPending,
  executionMessage,
  executionError,
  startContextDrift,
  onExecute,
  onResume,
}: ExecutionControlsProps) {
  return (
    <div className="grid gap-3 rounded border bg-background p-4">
      <div>
        <p className="text-sm font-medium">Execution Controls</p>
        <p className="text-sm text-muted-foreground">
          Run the approved chunks, or resume the run if it paused after an
          interruption or a failed chunk.
        </p>
      </div>

      {executionMessage && (
        <p className="text-sm font-medium text-green-600">
          {executionMessage}
        </p>
      )}
      {executionError && (
        <p className="text-sm font-medium text-red-500">
          {executionError}
        </p>
      )}
      {startContextDrift && (
        <div className="grid gap-3 rounded border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900">
          <div>
            <p className="font-semibold">Start context changed</p>
            {startContextDrift.message && (
              <p className="mt-1">{startContextDrift.message}</p>
            )}
            <p className="mt-1">
              Checkout the original start branch and execute again, or
              create a new run for the current branch.
            </p>
          </div>
          <div className="grid gap-2 text-xs sm:grid-cols-2">
            <div>
              <p className="font-medium">Planned against</p>
              <p className="font-mono">
                {startContextDrift.captured_start?.branch ??
                  'unknown'}
                @
                {startContextDrift.captured_start?.head_sha_short ??
                  'unknown'}
              </p>
            </div>
            <div>
              <p className="font-medium">Current checkout</p>
              <p className="font-mono">
                {startContextDrift.current?.branch ?? 'detached HEAD'}
                @
                {startContextDrift.current?.head_sha_short ??
                  'unknown'}
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <Button
          onClick={onExecute}
          disabled={actionPending}
          className="bg-blue-600 hover:bg-blue-700 text-white"
        >
          {isExecuting ? 'Executing...' : 'Execute Chunks'}
        </Button>
        <Button
          variant="outline"
          onClick={onResume}
          disabled={actionPending}
        >
          {isResuming ? 'Resuming...' : 'Resume Run'}
        </Button>
      </div>
    </div>
  )
}

// #22B scope-warning callout shown above a chunk's metadata. Display-only; #36B
// extraction (no visual/behavior change).
function ChunkScopeWarning({ scopeWarnings }: { scopeWarnings: string[] }) {
  return (
    <div className="mb-3 grid gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-900">
      <div className="flex items-center gap-2">
        <Badge
          variant="outline"
          className="border-amber-300 bg-amber-100 text-amber-800"
        >
          Scope warning
        </Badge>
      </div>
      <p>
        This chunk has a file-scope mismatch or constraint
        adjustment. Review Files Expected before approving.
      </p>
      <ul className="list-disc space-y-1 pl-5">
        {scopeWarnings.map((warning, index) => (
          <li key={index} className="break-words">
            {warning}
          </li>
        ))}
      </ul>
    </div>
  )
}

interface InlineChunkApprovalControlsProps {
  chunkNumber: number
  rejectReason: string
  onRejectReasonChange: (value: string) => void
  actionPending: boolean
  chunkActionPending: boolean
  approvingChunkNumber: number | null
  rejectingChunkNumber: number | null
  onApproveChunk: (chunkNumber: number) => void
  onRejectChunk: (chunkNumber: number, reason: string) => void
}

// Inline high-risk chunk approve/reject controls. #36B extraction (no
// visual/behavior change). The per-chunk reject reason stays owned by the parent
// and is passed in/out so state semantics are identical.
function InlineChunkApprovalControls({
  chunkNumber,
  rejectReason,
  onRejectReasonChange,
  actionPending,
  chunkActionPending,
  approvingChunkNumber,
  rejectingChunkNumber,
  onApproveChunk,
  onRejectChunk,
}: InlineChunkApprovalControlsProps) {
  return (
    <div className="grid gap-3 rounded border border-yellow-400 p-3">
      <div>
        <p className="font-medium">High-Risk Chunk Approval</p>
        <p className="text-muted-foreground">
          Review this chunk before continuing execution.
        </p>
      </div>

      <Textarea
        value={rejectReason}
        onChange={event => onRejectReasonChange(event.target.value)}
        placeholder="Optional rejection reason"
        disabled={actionPending}
      />

      <div className="flex flex-wrap gap-3">
        <Button
          onClick={() => onApproveChunk(chunkNumber)}
          disabled={actionPending}
          className="bg-green-600 hover:bg-green-700 text-white"
        >
          {chunkActionPending && approvingChunkNumber
            ? 'Approving...'
            : 'Approve Chunk'}
        </Button>
        <Button
          variant="destructive"
          onClick={() => onRejectChunk(chunkNumber, rejectReason)}
          disabled={actionPending}
        >
          {chunkActionPending && rejectingChunkNumber
            ? 'Rejecting...'
            : 'Reject Chunk'}
        </Button>
      </div>
    </div>
  )
}

// #36E: display-only framing around the existing ScopeExpansionBanner. It makes
// the file-permission-vs-code-approval distinction prominent BEFORE the banner.
// It wires nothing and changes no behavior — the banner keeps its own approve/
// reject controls and copy.
function ScopePermissionContext({ children }: { children: ReactNode }) {
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-amber-700">
          Scope permission request
        </h4>
        <span className="text-xs text-muted-foreground">
          file-permission decision — not code approval
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        Pipewright blocked a change that touched files outside this chunk&apos;s
        approved scope and committed nothing. Approving only adds the requested
        files to the allowed scope and retries — you will still review the
        resulting code before anything is committed.
      </p>
      {children}
    </div>
  )
}

// #36E: display-only framing around the existing PatchFailureBanner. `active`
// gates the heading so the banner stays a bare secondary diagnostic when a scope
// permission request owns the primary recovery path above it (#27F). It wires
// nothing and changes no behavior — retry eligibility stays inside the banner.
function PatchRecoveryContext({
  active,
  children,
}: {
  active: boolean
  children: ReactNode
}) {
  if (!active) return <>{children}</>
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-red-700">
          Patch recovery
        </h4>
        <span className="text-xs text-muted-foreground">
          nothing was committed
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        The code change could not be applied or was blocked, so nothing was
        committed. Retry is available only when Pipewright allows it; the full
        details and attempt history are below.
      </p>
      {children}
    </div>
  )
}

interface ChunkCardProps {
  chunk: ChunkStatus
  definition?: ChunkDefinition
  runId: string
  projectId: string
  actionPending: boolean
  approvingChunkNumber: number | null
  rejectingChunkNumber: number | null
  retryingChunkNumber: number | null
  hiddenApprovalChunkNumbers: number[]
  rejectReason: string
  onRejectReasonChange: (value: string) => void
  onApproveChunk: (chunkNumber: number) => void
  onRejectChunk: (chunkNumber: number, reason: string) => void
  onRetryChunk?: (chunkNumber: number, failureReportId: string) => void
  onScopeActionComplete?: () => void
}

// A single chunk's card: metadata, evidence (scope warning, runtime test
// validation, advisory review), recovery (scope expansion, patch failure,
// recovered marker, completion/error), and inline approval. #36B extraction of
// the original per-chunk map body (no visual/behavior change).
function ChunkCard({
  chunk,
  definition,
  runId,
  projectId,
  actionPending,
  approvingChunkNumber,
  rejectingChunkNumber,
  retryingChunkNumber,
  hiddenApprovalChunkNumbers,
  rejectReason,
  onRejectReasonChange,
  onApproveChunk,
  onRejectChunk,
  onRetryChunk,
  onScopeActionComplete,
}: ChunkCardProps) {
  const filesExpected =
    chunk.files_expected.length > 0
      ? chunk.files_expected
      : definition?.files_expected ?? []
  const dependsOn =
    chunk.depends_on.length > 0
      ? chunk.depends_on
      : definition?.depends_on ?? []
  const riskLevel =
    chunk.risk_level || definition?.risk_level || 'unknown'
  const requiresHumanReview =
    chunk.requires_human_review ||
    definition?.requires_human_review ||
    false
  const patchFailure = parsePatchFailureSummary(
    chunk.completion_summary
  )
  // #27F: a pending scope expansion request makes the scope banner the
  // primary action. When present we suppress the normal #26 Retry
  // button (a SCOPE_VIOLATION otherwise offers retry_with_instruction)
  // so the user is not nudged into the wrong recovery path.
  const pendingScope = chunk.pending_scope_expansion ?? null
  // #26E3: a recovered_patch_review summary is display-only context;
  // the awaiting_chunk_approval UI below still owns approve/commit.
  const recoveredReview = patchFailure
    ? null
    : parseRecoveredPatchReviewSummary(chunk.completion_summary)
  // #22B: surface backend [SCOPE] file-scope notes (#22A) so a
  // reviewer sees scope mismatches/adjustments before approving.
  // Read-only: this only displays existing data, never changes scope.
  const scopeWarnings = extractScopeWarnings(
    definition?.rationale,
    definition?.description
  )
  const hasScopeWarning = scopeWarnings.length > 0
  const isAwaitingChunkApproval =
    chunk.status === 'awaiting_chunk_approval'
  const showInlineChunkApproval =
    isAwaitingChunkApproval &&
    !patchFailure &&
    !hiddenApprovalChunkNumbers.includes(chunk.chunk_number)
  const chunkActionPending =
    approvingChunkNumber === chunk.chunk_number ||
    rejectingChunkNumber === chunk.chunk_number
  const chunkStatusDisplay = getStatusDisplay(chunk.status)

  return (
    <div className="rounded border bg-background p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            Chunk {chunk.chunk_number}: {chunk.title}
          </p>
          <p className="text-sm text-muted-foreground">
            {definition?.description || 'No description available.'}
          </p>
        </div>
        <Badge variant="outline" className={chunkStatusDisplay.className}>
          {chunkStatusDisplay.label}
        </Badge>
      </div>

      {hasScopeWarning && <ChunkScopeWarning scopeWarnings={scopeWarnings} />}

      <div className="grid gap-3 text-sm">
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <p className="font-medium">Risk Level</p>
            <p className="text-muted-foreground">{riskLevel}</p>
          </div>
          <div>
            <p className="font-medium">Token Estimate</p>
            <p className="text-muted-foreground">
              {definition?.token_estimate ?? 'Unknown'}
            </p>
          </div>
          <div>
            <p className="font-medium">Human Review</p>
            <p className="text-muted-foreground">
              {requiresHumanReview ? 'Required' : 'Not required'}
            </p>
          </div>
        </div>

        <div
          className={
            hasScopeWarning
              ? 'rounded border border-amber-300 bg-amber-50 px-3 py-2'
              : undefined
          }
        >
          <p className="flex items-center gap-2 font-medium">
            Files Expected
            {hasScopeWarning && (
              <Badge
                variant="outline"
                className="border-amber-300 bg-amber-100 text-amber-800"
              >
                Review scope
              </Badge>
            )}
          </p>
          <p
            className={
              hasScopeWarning
                ? 'break-words font-medium text-amber-900'
                : 'text-muted-foreground break-words'
            }
          >
            {formatList(filesExpected)}
          </p>
          {filesExpected.length === 0 && (
            <p className="mt-1 text-xs text-muted-foreground">
              Pipewright could not confidently map this chunk to
              indexed repository files, so no target files were
              approved. It is marked high-risk and requires human
              review — confirm the real target files before
              executing.
            </p>
          )}
        </div>

        <div>
          <p className="font-medium">Depends On</p>
          <p className="text-muted-foreground">
            {formatList(dependsOn)}
          </p>
        </div>

        {definition?.rationale && (
          <div>
            <p className="font-medium">Rationale</p>
            <p className="text-muted-foreground whitespace-pre-wrap">
              {definition.rationale}
            </p>
          </div>
        )}

        {/* #28E: display-only runtime test verdict for this chunk.
            Renders nothing until a verdict is recorded (pending/old
            chunks). Never gates approval, commit, or PR. */}
        <RuntimeTestValidationBanner
          validation={chunk.test_validation}
        />

        {/* Advisory AI Review (display-only). Renders only when a
            review exists; never gates approval, exposes no actions,
            and is shown below the test-validation banner / above the
            chunk's approval controls. */}
        <AdvisoryReviewPanel review={chunk.review} />

        {pendingScope && (
          // #27F: primary recovery action for a pending scope
          // expansion. Rendered above the diagnostic patch-failure
          // banner; owns its own approve/reject + error display.
          // #36E: wrapped in display-only scope-permission framing.
          <ScopePermissionContext>
            <ScopeExpansionBanner
              runId={runId}
              chunkNumber={chunk.chunk_number}
              request={pendingScope}
              originalFiles={filesExpected}
              diagnosticSummary={patchFailure?.message ?? null}
              onActionComplete={onScopeActionComplete}
            />
          </ScopePermissionContext>
        )}

        {patchFailure ? (
          // Structured patch failure (#18E): the banner shows the
          // message, so skip the raw completion_summary dump and the
          // generic error_message block to avoid duplication. When a
          // scope expansion is pending we pass no onRetry so the normal
          // #26 Retry button is not shown as the primary action (#27F).
          // #36E: wrap in patch-recovery framing, but only when scope is
          // not the primary path (active={!pendingScope}).
          <PatchRecoveryContext active={!pendingScope}>
            <PatchFailureBanner
              report={patchFailure}
              projectId={projectId}
              chunkNumber={chunk.chunk_number}
              chunkStatus={chunk.status}
              onRetry={pendingScope ? undefined : onRetryChunk}
              isRetrying={retryingChunkNumber === chunk.chunk_number}
            />
          </PatchRecoveryContext>
        ) : recoveredReview ? (
          // Recovered patch awaiting review (#26E3): show a marker
          // instead of dumping the raw recovered_patch_review JSON.
          <RecoveredReviewMarker
            summary={recoveredReview}
            validation={chunk.test_validation}
          />
        ) : (
          <>
            {chunk.completion_summary && (
              <div>
                <p className="font-medium">Completion Summary</p>
                <p className="text-muted-foreground whitespace-pre-wrap">
                  {chunk.completion_summary}
                </p>
              </div>
            )}

            {chunk.error_message && (
              <div>
                <p className="font-medium text-red-500">Error</p>
                <p className="text-red-500 whitespace-pre-wrap">
                  {chunk.error_message}
                </p>
              </div>
            )}
          </>
        )}

        {showInlineChunkApproval && (
          <InlineChunkApprovalControls
            chunkNumber={chunk.chunk_number}
            rejectReason={rejectReason}
            onRejectReasonChange={onRejectReasonChange}
            actionPending={actionPending}
            chunkActionPending={chunkActionPending}
            approvingChunkNumber={approvingChunkNumber}
            rejectingChunkNumber={rejectingChunkNumber}
            onApproveChunk={onApproveChunk}
            onRejectChunk={onRejectChunk}
          />
        )}
      </div>
    </div>
  )
}

// #36D: collapse non-attention chunks (completed-not-current, future pending)
// into a compact row by default, with the full details one click away. Used ONLY
// for chunks with no pending decision/control; attention/decision chunks render
// the full ChunkCard directly (never collapsible). This is presentation-only: the
// expanded view is the exact same ChunkCard with the same controls and handlers,
// so no behavior, wiring, or banner placement changes.
function CollapsibleChunkCard(props: ChunkCardProps) {
  const { chunk, definition } = props
  const [expanded, setExpanded] = useState(false)

  if (expanded) {
    return (
      <div className="grid gap-2">
        <ChunkCard {...props} />
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setExpanded(false)}
            aria-expanded={true}
          >
            Hide details
          </Button>
        </div>
      </div>
    )
  }

  const statusDisplay = getStatusDisplay(chunk.status)
  const filesExpected =
    chunk.files_expected.length > 0
      ? chunk.files_expected
      : definition?.files_expected ?? []
  const riskLevel = chunk.risk_level || definition?.risk_level || 'unknown'

  const testVerdict = chunk.test_validation?.verdict
  const testMeta = testVerdict
    ? ACTIVE_TEST_SUMMARY[testVerdict] ?? ACTIVE_TEST_SUMMARY.unknown
    : null
  const review = chunk.review
  const reviewMeta =
    review && review.review_status === 'completed' && review.verdict
      ? ACTIVE_REVIEW_SUMMARY[review.verdict] ?? null
      : null

  return (
    <div className="rounded border bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            Chunk {chunk.chunk_number}: {chunk.title}
          </p>
          <p className="text-sm text-muted-foreground">
            {definition?.description || 'No description available.'}
          </p>
        </div>
        <Badge variant="outline" className={statusDisplay.className}>
          {statusDisplay.label}
        </Badge>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>
          <span className="font-medium text-foreground">Risk:</span> {riskLevel}
        </span>
        <span className="break-words">
          <span className="font-medium text-foreground">Files:</span>{' '}
          {truncateFiles(filesExpected)}
        </span>
        {testMeta && (
          <Badge variant="outline" className={testMeta.className}>
            {testMeta.label}
          </Badge>
        )}
        {reviewMeta && (
          <Badge variant="outline" className={reviewMeta.className}>
            {reviewMeta.label}
          </Badge>
        )}
      </div>

      <div className="mt-3">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setExpanded(true)}
          aria-expanded={false}
        >
          Show details
        </Button>
      </div>
    </div>
  )
}

interface PlanApprovalControlsProps {
  rejectReason: string
  onRejectReasonChange: (value: string) => void
  error: string | null
  isApproving: boolean
  isRejecting: boolean
  actionPending: boolean
  onApprove: () => void
  onReject: (reason: string) => void
}

// Plan-level approve/reject controls (awaiting_approval). #36B extraction (no
// visual/behavior change). The reject reason stays owned by the parent.
function PlanApprovalControls({
  rejectReason,
  onRejectReasonChange,
  error,
  isApproving,
  isRejecting,
  actionPending,
  onApprove,
  onReject,
}: PlanApprovalControlsProps) {
  return (
    <div className="grid gap-3">
      <Textarea
        value={rejectReason}
        onChange={event => onRejectReasonChange(event.target.value)}
        placeholder="Optional rejection reason"
        disabled={actionPending}
      />

      {error && (
        <p className="text-sm font-medium text-red-500">{error}</p>
      )}

      <div className="flex flex-wrap gap-3">
        <Button
          onClick={onApprove}
          disabled={actionPending}
          className="bg-green-600 hover:bg-green-700 text-white"
        >
          {isApproving ? 'Approving...' : 'Approve Plan'}
        </Button>
        <Button
          variant="destructive"
          onClick={() => onReject(rejectReason)}
          disabled={actionPending}
        >
          {isRejecting ? 'Rejecting...' : 'Reject Plan'}
        </Button>
      </div>
    </div>
  )
}

// #36C: display-only "current chunk at a glance" summary rendered ABOVE the full
// chunk list. It contains NO action buttons and wires nothing — every real
// control and full banner stays in the ChunkCard list below, which remains the
// source of truth. It must not duplicate the long banners (test validation,
// advisory review, scope expansion, patch failure); it only summarizes them.
function ActiveChunkCard({
  chunk,
  definition,
}: {
  chunk: ChunkStatus
  definition?: ChunkDefinition
}) {
  const statusDisplay = getStatusDisplay(chunk.status)
  const filesExpected =
    chunk.files_expected.length > 0
      ? chunk.files_expected
      : definition?.files_expected ?? []
  const riskLevel = chunk.risk_level || definition?.risk_level || 'unknown'
  const requiresHumanReview =
    chunk.requires_human_review || definition?.requires_human_review || false

  // Same pure parsing used by ChunkCard, here only to produce a one-line status
  // chip — the actual banners still render in full below.
  const patchFailure = parsePatchFailureSummary(chunk.completion_summary)
  const recoveredReview = patchFailure
    ? null
    : parseRecoveredPatchReviewSummary(chunk.completion_summary)
  const pendingScope = chunk.pending_scope_expansion ?? null

  const chips: Array<{ key: string; label: string; className: string }> = []

  const testVerdict = chunk.test_validation?.verdict
  if (testVerdict) {
    chips.push({
      key: 'tests',
      ...(ACTIVE_TEST_SUMMARY[testVerdict] ?? ACTIVE_TEST_SUMMARY.unknown),
    })
  }

  const review = chunk.review
  if (review && review.review_status === 'completed' && review.verdict) {
    const meta = ACTIVE_REVIEW_SUMMARY[review.verdict]
    if (meta) chips.push({ key: 'review', ...meta })
  }

  if (pendingScope) {
    chips.push({
      key: 'scope',
      label: 'Scope: expansion needs approval',
      className: 'border-amber-300 bg-amber-100 text-amber-800',
    })
  }

  if (patchFailure || chunk.error_message) {
    chips.push({
      key: 'failure',
      label: 'Has a recorded failure',
      className: 'border-red-300 bg-red-100 text-red-800',
    })
  } else if (recoveredReview) {
    chips.push({
      key: 'recovered',
      label: 'Recovered patch awaiting review',
      className: 'border-green-300 bg-green-100 text-green-800',
    })
  } else if (chunk.completion_summary) {
    chips.push({
      key: 'completion',
      label: 'Completion summary available',
      className: 'border-slate-300 bg-slate-100 text-slate-600',
    })
  }

  return (
    <div className="grid gap-3 rounded-lg border bg-muted/30 p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Current chunk at a glance
        </h3>
        <span className="text-xs text-muted-foreground">read-only summary</span>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold">
            Chunk {chunk.chunk_number}: {chunk.title}
          </p>
          <p className="text-sm text-muted-foreground">
            {definition?.description || 'No description available.'}
          </p>
        </div>
        <Badge variant="outline" className={statusDisplay.className}>
          {statusDisplay.label}
        </Badge>
      </div>

      <div className="grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <p className="font-medium">Files Expected</p>
          <p className="text-muted-foreground break-words">
            {truncateFiles(filesExpected)}
          </p>
        </div>
        <div>
          <p className="font-medium">Risk Level</p>
          <p className="text-muted-foreground">{riskLevel}</p>
        </div>
        <div>
          <p className="font-medium">Human Review</p>
          <p className="text-muted-foreground">
            {requiresHumanReview ? 'Required' : 'Not required'}
          </p>
        </div>
      </div>

      {chips.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {chips.map(chip => (
            <Badge key={chip.key} variant="outline" className={chip.className}>
              {chip.label}
            </Badge>
          ))}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        Read-only summary. Full chunk details remain below — use the controls in
        the full chunk details below to act.
      </p>
    </div>
  )
}

export default function ChunkPlanPanel({
  plan,
  isApproving,
  isRejecting,
  isExecuting,
  isResuming,
  approvingChunkNumber,
  rejectingChunkNumber,
  error,
  executionMessage,
  executionError,
  startContextDrift = null,
  chunkActionMessage,
  chunkActionError,
  hiddenApprovalChunkNumbers = [],
  retryingChunkNumber = null,
  onScopeActionComplete,
  onApprove,
  onReject,
  onExecute,
  onResume,
  onApproveChunk,
  onRejectChunk,
  onRetryChunk,
}: ChunkPlanPanelProps) {
  const [rejectReason, setRejectReason] = useState('')
  const [chunkRejectReasons, setChunkRejectReasons] = useState<
    Record<number, string>
  >({})
  const definitionsByNumber = new Map<number, ChunkDefinition>(
    (plan.triage?.chunks ?? []).map(chunk => [chunk.chunk_number, chunk])
  )
  const isAwaitingApproval = plan.chunk_plan_status === 'awaiting_approval'
  const isApproved = plan.chunk_plan_status === 'approved'
  const actionPending =
    isApproving ||
    isRejecting ||
    isExecuting ||
    isResuming ||
    approvingChunkNumber !== null ||
    rejectingChunkNumber !== null
  const planStatusDisplay = getStatusDisplay(plan.chunk_plan_status)
  // #36C: display-only summary of the current/active chunk; null = render nothing.
  const activeChunk = selectActiveChunk(plan)

  return (
    <Card className="mb-4">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Chunk Plan</CardTitle>
            <CardDescription>
              Review the planned execution chunks before approving.
            </CardDescription>
          </div>
          <Badge variant="outline" className={planStatusDisplay.className}>
            {planStatusDisplay.label}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <ChunkPlanSummary plan={plan} />

        <Separator />

        {isApproved && (
          <>
            <ExecutionControls
              isExecuting={isExecuting}
              isResuming={isResuming}
              actionPending={actionPending}
              executionMessage={executionMessage}
              executionError={executionError}
              startContextDrift={startContextDrift}
              onExecute={onExecute}
              onResume={onResume}
            />

            <Separator />
          </>
        )}

        {activeChunk && (
          <ActiveChunkCard
            chunk={activeChunk}
            definition={getChunkDefinition(activeChunk, definitionsByNumber)}
          />
        )}

        <div className="grid gap-3">
          {plan.chunks.map(chunk => {
            const cardProps: ChunkCardProps = {
              chunk,
              definition: getChunkDefinition(chunk, definitionsByNumber),
              runId: plan.run_id,
              projectId: plan.project_id,
              actionPending,
              approvingChunkNumber,
              rejectingChunkNumber,
              retryingChunkNumber,
              hiddenApprovalChunkNumbers,
              rejectReason: chunkRejectReasons[chunk.chunk_number] ?? '',
              onRejectReasonChange: value =>
                setChunkRejectReasons(previous => ({
                  ...previous,
                  [chunk.chunk_number]: value,
                })),
              onApproveChunk,
              onRejectChunk,
              onRetryChunk,
              onScopeActionComplete,
            }
            // #36D: attention/decision chunks render the full ChunkCard and stay
            // expanded; others collapse to a compact row with details one click
            // away. Same props/handlers either way — presentation only.
            return chunkNeedsAttention(
              chunk,
              activeChunk?.chunk_number ?? null
            ) ? (
              <ChunkCard key={chunk.chunk_number} {...cardProps} />
            ) : (
              <CollapsibleChunkCard key={chunk.chunk_number} {...cardProps} />
            )
          })}
        </div>

        {(chunkActionMessage || chunkActionError) && (
          <div>
            {chunkActionMessage && (
              <p className="text-sm font-medium text-green-600">
                {chunkActionMessage}
              </p>
            )}
            {chunkActionError && (
              <p className="text-sm font-medium text-red-500">
                {chunkActionError}
              </p>
            )}
          </div>
        )}

        {isAwaitingApproval && (
          <>
            <Separator />

            <PlanApprovalControls
              rejectReason={rejectReason}
              onRejectReasonChange={setRejectReason}
              error={error}
              isApproving={isApproving}
              isRejecting={isRejecting}
              actionPending={actionPending}
              onApprove={onApprove}
              onReject={onReject}
            />
          </>
        )}
      </CardContent>
    </Card>
  )
}
