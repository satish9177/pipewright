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

export default function RuntimeTestValidationBanner({
  validation,
}: RuntimeTestValidationBannerProps) {
  if (!validation) return null

  const { verdict, reason, zero_tests_detected } = validation

  if (verdict === 'strong') {
    const counts = passedCountText(validation)
    return (
      <div className="grid gap-1 rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800">
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className="border-green-300 bg-green-100 text-green-800"
          >
            Runtime test validation: strong
          </Badge>
        </div>
        <p>
          This change ran real tests that passed
          {counts ? ` (${counts})` : ''}.
        </p>
      </div>
    )
  }

  // weak / none / unknown all use the amber card; copy differs per verdict.
  const isUnknown = verdict === 'unknown'
  const badgeLabel =
    verdict === 'weak'
      ? 'Weak test validation'
      : verdict === 'none'
        ? 'No tests ran'
        : 'Unverified test run'

  let headline: string
  if (verdict === 'weak') {
    headline = 'Tests did not meaningfully validate this change.'
  } else if (verdict === 'none') {
    headline = 'No test command ran for this chunk.'
  } else {
    headline =
      'Pipewright could not confirm whether this command ran real tests. ' +
      'Review the output manually.'
  }

  return (
    <div className="grid gap-1 rounded border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900">
      <div className="flex items-center gap-2">
        <Badge
          variant="outline"
          className="border-amber-300 bg-amber-100 text-amber-800"
        >
          {badgeLabel}
        </Badge>
      </div>
      <p>{headline}</p>
      {verdict === 'weak' && zero_tests_detected && (
        <p className="text-amber-800">
          The test runner reported zero tests.
        </p>
      )}
      {reason && <p className="text-amber-800">{reason}</p>}
      {!isUnknown && (
        <p className="text-xs text-amber-700">
          This is informational only and does not block approval yet.
        </p>
      )}
    </div>
  )
}
