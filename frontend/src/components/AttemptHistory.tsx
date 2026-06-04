// Display-only recovery attempt history (#26E3).
//
// Renders the bounded attempts[] recorded on a patch_failure report or a
// recovered_patch_review summary. Intentionally compact and read-only: it shows
// only safe, already-present diagnostics (number, mode, outcome, failure type,
// test outcome, timestamp) and never file contents, old_string/new_string, raw
// model output, or large changed-files blobs.

import type { TestRunVerdict } from '@/api/client'
import type { PatchRecoveryAttempt } from '@/utils/patchFailure'

interface AttemptHistoryProps {
  attempts?: PatchRecoveryAttempt[]
  // Runtime test-validation verdict for the chunk these attempts belong to.
  // Only a "strong" verdict may render a passing attempt as "Tests passed";
  // weak/none/unknown or a missing verdict stays cautious.
  validationVerdict?: TestRunVerdict | null
}

const RECOVERY_MODE_LABELS: Record<string, string> = {
  initial: 'Initial attempt',
  human: 'Manual retry',
  human_with_instruction: 'Manual retry (with instruction)',
  auto: 'Automatic retry',
}

const OUTCOME_LABELS: Record<string, string> = {
  failed: 'Failed',
  recovered: 'Recovered',
  manual_intervention: 'Manual intervention',
}

const TEST_OUTCOME_LABELS: Record<string, string> = {
  passed: 'Tests passed',
  failed: 'Tests failed',
  not_run: 'Tests not run',
}

// Label a passing attempt cautiously unless runtime validation was strong. The
// command may have exited 0 without meaningful tests (e.g. `python --version`).
function testOutcomeLabel(
  outcome: string,
  validationVerdict?: TestRunVerdict | null
): string {
  if (outcome === 'passed' && validationVerdict !== 'strong') {
    return 'Command completed · weak validation'
  }
  return TEST_OUTCOME_LABELS[outcome] ?? outcome
}

function formatStartedAt(value: string | undefined): string | null {
  if (!value) return null
  const date = new Date(value)
  // started_at is an ISO/UTC string from the backend; format defensively and
  // fall back to the raw value rather than rendering "Invalid Date".
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export default function AttemptHistory({
  attempts,
  validationVerdict,
}: AttemptHistoryProps) {
  if (!attempts || attempts.length === 0) return null

  return (
    <div className="grid gap-2 text-sm">
      <p className="font-medium">Recovery attempts</p>
      <ul className="grid gap-2">
        {attempts.map(attempt => {
          const mode =
            RECOVERY_MODE_LABELS[attempt.recovery_mode] ?? attempt.recovery_mode
          const outcome = attempt.outcome
            ? OUTCOME_LABELS[attempt.outcome] ?? attempt.outcome
            : null
          const testOutcome = attempt.test_outcome
            ? testOutcomeLabel(attempt.test_outcome, validationVerdict)
            : null
          const startedAt = formatStartedAt(attempt.started_at)

          return (
            <li
              key={attempt.attempt_id ?? attempt.attempt_number}
              className="rounded border bg-muted/40 px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">
                  Attempt {attempt.attempt_number}
                </span>
                <span className="text-muted-foreground">{mode}</span>
                {outcome && (
                  <span className="text-muted-foreground">· {outcome}</span>
                )}
              </div>
              {(attempt.failure_type || testOutcome || startedAt) && (
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
                  {attempt.failure_type && <span>{attempt.failure_type}</span>}
                  {testOutcome && <span>{testOutcome}</span>}
                  {startedAt && <span>{startedAt}</span>}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
