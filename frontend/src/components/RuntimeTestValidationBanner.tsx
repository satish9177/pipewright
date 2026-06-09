import type { TestRunValidation } from '@/api/client'
import { Badge } from '@/components/ui/badge'

interface RuntimeTestValidationBannerProps {
  validation?: TestRunValidation | null
}

/**
 * Display-only runtime test-validation banner (#28E).
 *
 * Surfaces the per-chunk verdict persisted in #28D (strong / weak / none /
 * unknown) for the human reviewing a completed or recovered chunk. It changes
 * nothing: it never gates final approval, never disables commit/PR, and adds no
 * acknowledgement. The future #28F slice owns enforcement.
 *
 * This is the chunk-level "this run's tests" signal, distinct from the
 * project-level "your configured command looks weak" hint in
 * TestCommandQualityWarning. `strong` renders a quiet positive note; the rest
 * render an amber/neutral card with the backend reason. `unknown` is phrased as
 * "could not confirm" and never accuses a custom script of being weak.
 */
function passedCountText(validation: TestRunValidation): string | null {
  if (!validation.counts_parsed) return null
  const passed = validation.passed_tests
  if (typeof passed !== 'number') return null
  return `${passed} test${passed === 1 ? '' : 's'} passed`
}

// #38A: a compact, focal "big number" for the validation verdict so a human can
// read strong/weak/none at a glance. Display-only — it summarizes the SAME
// persisted counts the banner already showed; it never gates or acknowledges
// anything. Strong shows passed/total (or the passed count); weak/none/unknown
// deliberately read as a stark "0" / "—" / "?" so the gap is never minimized.
function verdictBigNumber(validation: TestRunValidation): string {
  const { verdict, passed_tests, total_tests, counts_parsed } = validation
  if (verdict === 'none') return '0'
  if (verdict === 'unknown') return '?'
  if (verdict === 'weak' && validation.zero_tests_detected) return '0'
  if (counts_parsed && typeof passed_tests === 'number') {
    return typeof total_tests === 'number'
      ? `${passed_tests}/${total_tests}`
      : `${passed_tests}`
  }
  return verdict === 'strong' ? '✓' : '—'
}

export default function RuntimeTestValidationBanner({
  validation,
}: RuntimeTestValidationBannerProps) {
  if (!validation) return null

  const { verdict, reason, zero_tests_detected } = validation

  if (verdict === 'strong') {
    const counts = passedCountText(validation)
    return (
      <div className="rounded border border-green-200 bg-green-50 px-3 py-2.5 text-sm text-green-800">
        <div className="flex items-center gap-3">
          <span
            className="shrink-0 font-mono text-3xl font-semibold leading-none text-green-700"
            aria-hidden="true"
          >
            {verdictBigNumber(validation)}
          </span>
          <div className="grid gap-1">
            <Badge
              variant="outline"
              className="w-fit border-green-300 bg-green-100 text-green-800"
            >
              Runtime test validation: strong
            </Badge>
            <p>
              Tests ran and passed{counts ? ` (${counts})` : ''} — meaningful
              evidence for this change, though not proof it is correct.
            </p>
          </div>
        </div>
      </div>
    )
  }

  // weak / none / unknown all use the amber card; copy differs per verdict.
  const badgeLabel =
    verdict === 'weak'
      ? 'Weak test validation'
      : verdict === 'none'
        ? 'No tests ran'
        : 'Unverified test run'

  // Headline = what happened; guidance = what the user can do about it. Copy is
  // action-oriented and honest: it never claims tests passed or that Pipewright
  // verified the change.
  let headline: string
  let guidance: string
  if (verdict === 'weak') {
    headline = 'Tests ran, but they may not prove this change works.'
    guidance =
      'Review the command and output before final approval — you may need a ' +
      'stronger, project-specific test command.'
  } else if (verdict === 'none') {
    headline = 'No meaningful tests ran for this change.'
    guidance =
      'Pipewright cannot verify this change from tests. Continue only if you ' +
      'intentionally accept that limitation; adding tests or a real test ' +
      'command would give stronger evidence.'
  } else {
    headline = 'Test evidence could not be classified.'
    guidance =
      'Tests may have run, but Pipewright could not determine how much ' +
      'confidence they provide. Review the output manually.'
  }

  // Honest tie to the EXISTING acknowledgement gate (#28F/#28G): weak/none block
  // final approval until acknowledged. Display-only copy that enforces nothing —
  // TestValidationAckPanel remains the actual gate UI at final approval.
  const requiresAck = validation.requires_acknowledgement === true

  // Keep weak/none/unknown unmistakably warning-like (amber card). The focal
  // number is red for "none" (no tests at all) and amber otherwise, so the gap
  // is never visually softened into something reassuring.
  const bigNumberColor = verdict === 'none' ? 'text-red-700' : 'text-amber-700'

  return (
    <div className="rounded border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900">
      <div className="flex items-start gap-3">
        <span
          className={`shrink-0 font-mono text-3xl font-semibold leading-none ${bigNumberColor}`}
          aria-hidden="true"
        >
          {verdictBigNumber(validation)}
        </span>
        <div className="grid gap-1">
          <Badge
            variant="outline"
            className="w-fit border-amber-300 bg-amber-100 text-amber-800"
          >
            {badgeLabel}
          </Badge>
          <p>{headline}</p>
          <p className="text-amber-800">{guidance}</p>
          {verdict === 'weak' && zero_tests_detected && (
            <p className="text-amber-800">
              The test runner reported zero tests.
            </p>
          )}
          {reason && <p className="text-amber-800">{reason}</p>}
          {requiresAck && (
            <p className="text-xs text-amber-700">
              Final approval requires acknowledging this limitation first.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
