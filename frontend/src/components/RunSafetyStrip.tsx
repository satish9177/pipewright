import type {
  ChunkPlanResponse,
  ChunkStatus,
  OperatorState,
  Project,
  Run,
} from '@/api/client'

// #35E: read-only safety/trust strip for Run Detail. It summarizes safety
// signals that already exist on the run/chunk read model into compact, plain-
// language chips near the top of the page. It is DISPLAY-ONLY: it renders no
// actions, wires no mutations, fetches nothing, and never replaces or hides the
// detailed banners/cards below — those remain the source of truth. Unknown or
// unverified states are shown as such and never imply safety (fail-closed).

type ChipTone = 'ok' | 'warn' | 'alert' | 'neutral'

interface SafetyChip {
  key: string
  // Plain-language category, e.g. "Scope".
  label: string
  // Plain-language value, e.g. "OK" / "Weak" / "Unknown".
  value: string
  tone: ChipTone
  // Tooltip that explains the chip and points at the detailed section below.
  hint: string
}

const TONE_CLASS: Record<ChipTone, string> = {
  ok: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  warn: 'border-amber-300 bg-amber-50 text-amber-900',
  alert: 'border-red-300 bg-red-50 text-red-800',
  neutral: 'border-slate-200 bg-slate-100 text-slate-700',
}

// Scope: is the change still inside the approved chunk scope, or is a human
// approval pending? Derived from pending scope-expansion + run status.
function scopeChip(
  run: Run,
  chunkPlan: ChunkPlanResponse | undefined,
  chunks: ChunkStatus[],
): SafetyChip {
  const hasPendingScope =
    run.status === 'awaiting_scope_approval' ||
    chunks.some(chunk => chunk.pending_scope_expansion)
  if (hasPendingScope) {
    return {
      key: 'scope',
      label: 'Scope',
      value: 'Expanded — needs approval',
      tone: 'warn',
      hint: 'A scope expansion is pending. Review it in the Chunk Plan Details below.',
    }
  }
  if (run.status === 'awaiting_chunk_plan_approval') {
    return {
      key: 'scope',
      label: 'Scope',
      value: 'Needs approval',
      tone: 'warn',
      hint: 'The chunk plan is awaiting your approval in the Chunk Plan Details below.',
    }
  }
  if (chunkPlan) {
    return {
      key: 'scope',
      label: 'Scope',
      value: 'OK',
      tone: 'ok',
      hint: 'No scope expansion is pending; edits stayed within the approved chunks.',
    }
  }
  return {
    key: 'scope',
    label: 'Scope',
    value: 'Unknown',
    tone: 'neutral',
    hint: 'No chunk plan is loaded yet, so scope status cannot be summarized.',
  }
}

// Tests: worst runtime test-validation verdict across chunks (#28E). Display-
// only; this never proves code correctness and never gates approval.
function testsChip(chunks: ChunkStatus[]): SafetyChip {
  const verdicts = chunks
    .map(chunk => chunk.test_validation?.verdict)
    .filter((verdict): verdict is NonNullable<typeof verdict> => Boolean(verdict))

  if (verdicts.includes('none')) {
    return {
      key: 'tests',
      label: 'Tests',
      value: 'No tests',
      tone: 'warn',
      hint: 'A chunk ran with no meaningful tests. See the chunk plan and final approval.',
    }
  }
  if (verdicts.includes('weak')) {
    return {
      key: 'tests',
      label: 'Tests',
      value: 'Weak',
      tone: 'warn',
      hint: 'Runtime test validation was weak and needs acknowledgement before final approval.',
    }
  }
  if (verdicts.includes('unknown')) {
    return {
      key: 'tests',
      label: 'Tests',
      value: 'Unknown',
      tone: 'neutral',
      hint: 'Pipewright could not confirm whether the command ran a test suite.',
    }
  }
  if (verdicts.length === 0) {
    return {
      key: 'tests',
      label: 'Tests',
      value: 'Pending',
      tone: 'neutral',
      hint: 'No runtime test verdict has been recorded for this run yet.',
    }
  }
  return {
    key: 'tests',
    label: 'Tests',
    value: 'Strong',
    tone: 'ok',
    hint: 'Meaningful tests ran and passed per Pipewright process rules; this does not prove correctness.',
  }
}

// Review: advisory adversarial-reviewer overlay + reviewer independence (#33C),
// when present on the chunk read model. Advisory only; grants no authority.
function reviewChip(chunks: ChunkStatus[]): SafetyChip {
  const reviews = chunks
    .map(chunk => chunk.review)
    .filter((review): review is NonNullable<typeof review> => Boolean(review))

  if (reviews.length === 0) {
    return {
      key: 'review',
      label: 'Review',
      value: 'Not available',
      tone: 'neutral',
      hint: 'No advisory AI review is attached to this run.',
    }
  }
  if (
    reviews.some(
      review =>
        review.verdict === 'needs_human_attention' || review.verdict === 'risky',
    )
  ) {
    return {
      key: 'review',
      label: 'Review',
      value: 'Needs attention',
      tone: 'warn',
      hint: 'The advisory reviewer flagged something for a human. See the chunk plan.',
    }
  }
  const independence = reviews
    .map(review => review.reviewer_independence?.status)
    .filter((status): status is NonNullable<typeof status> => Boolean(status))
  if (independence.includes('self_review')) {
    return {
      key: 'review',
      label: 'Review',
      value: 'Self-review',
      tone: 'warn',
      hint: 'The coder and reviewer used the same provider/model, so the review is not independent.',
    }
  }
  if (independence.includes('independent')) {
    return {
      key: 'review',
      label: 'Review',
      value: 'Independent',
      tone: 'ok',
      hint: 'The advisory review ran on a different provider/model than the coder.',
    }
  }
  return {
    key: 'review',
    label: 'Review',
    value: 'Unknown',
    tone: 'neutral',
    hint: 'An advisory review exists but its independence could not be determined.',
  }
}

// PR: pull-request lifecycle from the run read model. Live GitHub check results
// (passed/failed/pending) are intentionally NOT summarized here — they come from
// the on-demand "Refresh PR checks" action and are not on the read model.
function prChip(run: Run, prMode?: string): SafetyChip {
  if (run.push_error || run.status === 'push_failed') {
    return {
      key: 'pr',
      label: 'PR',
      value: 'Push failed',
      tone: 'alert',
      hint: 'The last push/PR attempt failed. See Final Approval and PR below.',
    }
  }
  if (run.pr_url) {
    return {
      key: 'pr',
      label: 'PR',
      value: 'PR open',
      tone: 'ok',
      hint: 'A pull request is open. Pipewright never merges automatically.',
    }
  }
  if (run.status === 'final_approved' || run.status === 'pushing') {
    // local_only never pushes or opens a PR in-app, so a plain "Ready" here would
    // wrongly imply an in-app push/PR is queued. Surface it as Local-only and
    // point at the manual/out-of-app flow instead.
    if (prMode === 'local_only') {
      return {
        key: 'pr',
        label: 'PR',
        value: 'Local-only',
        tone: 'neutral',
        hint: 'Local-only mode creates no in-app PR. Your changes stay on the local branch — push manually outside Pipewright.',
      }
    }
    return {
      key: 'pr',
      label: 'PR',
      value: 'Ready',
      tone: 'neutral',
      hint: 'Final approval is in; push/PR is the next step in Final Approval and PR.',
    }
  }
  return {
    key: 'pr',
    label: 'PR',
    value: 'Not ready',
    tone: 'neutral',
    hint: 'No push or pull request yet. This is expected until final approval.',
  }
}

// State: whether Pipewright recognizes the current run state. Fail-closed: an
// unknown state is surfaced as a warning and never implies safety.
function stateChip(operatorState: OperatorState | null | undefined): SafetyChip | null {
  if (!operatorState) return null
  if (operatorState.unknown_state_warning) {
    return {
      key: 'state',
      label: 'State',
      value: 'Unknown state',
      tone: 'warn',
      hint: operatorState.unknown_state_warning,
    }
  }
  return {
    key: 'state',
    label: 'State',
    value: 'Clear',
    tone: 'ok',
    hint: 'Pipewright recognizes the current run state.',
  }
}

function WarnIcon() {
  return (
    <svg
      className="h-3 w-3 shrink-0"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  )
}

export default function RunSafetyStrip({
  run,
  chunkPlan,
  project,
}: {
  run: Run
  chunkPlan?: ChunkPlanResponse
  project?: Project
}) {
  const chunks = chunkPlan?.chunks ?? []
  const chips: SafetyChip[] = [
    scopeChip(run, chunkPlan, chunks),
    testsChip(chunks),
    reviewChip(chunks),
    prChip(run, project?.pr_mode),
  ]
  const state = stateChip(chunkPlan?.operator_state)
  if (state) chips.push(state)

  return (
    <section className="mb-6" aria-label="Run safety overview">
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Safety overview
        </h3>
        <span className="text-xs text-muted-foreground">
          read-only summary of existing signals
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {chips.map(chip => {
          const showIcon = chip.tone === 'warn' || chip.tone === 'alert'
          return (
            <span
              key={chip.key}
              title={chip.hint}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${TONE_CLASS[chip.tone]}`}
            >
              {showIcon && <WarnIcon />}
              <span className="font-medium">{chip.label}:</span>
              <span>{chip.value}</span>
            </span>
          )
        })}
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        This summarizes signals shown in full below. The detailed cards remain the
        source of truth — nothing here changes run behavior.
      </p>
    </section>
  )
}
