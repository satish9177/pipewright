import type { Project } from '@/api/client'
import { Badge } from '@/components/ui/badge'

interface TestCommandQualityWarningProps {
  project: Project | null | undefined
  // 'settings' = next to the Test Command input (configuration).
  // 'review'   = near final approval / run review (before PR/final approval).
  context: 'settings' | 'review'
}

/**
 * Surfaces the backend-computed test-command quality (#23A/#23B).
 *
 * Display-only: it reads `project.test_command_quality` and never changes
 * execution, approval, or backend behavior. `weak` shows a prominent amber
 * banner; `unknown` shows a subtle muted hint; `likely_test` (or missing)
 * renders nothing.
 */
export default function TestCommandQualityWarning({
  project,
  context,
}: TestCommandQualityWarningProps) {
  if (!project) return null

  const quality = project.test_command_quality
  const reason = project.test_command_quality_reason
  const command = project.test_command

  if (quality === 'weak') {
    return (
      <div className="grid gap-1 rounded border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900">
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className="border-amber-300 bg-amber-100 text-amber-800"
          >
            Weak test validation
          </Badge>
        </div>
        <p>
          {context === 'settings'
            ? 'This command may not run project tests.'
            : 'The configured test command does not appear to run project tests.'}
        </p>
        {command && (
          <p className="break-words font-mono text-xs">{command}</p>
        )}
        {reason && <p className="text-amber-800">{reason}</p>}
        {context === 'review' && (
          <p className="text-amber-800">
            The command may have exited successfully, but this should not be
            treated as strong test coverage.
          </p>
        )}
      </div>
    )
  }

  if (quality === 'unknown') {
    return (
      <p className="text-xs text-muted-foreground">
        {context === 'settings'
          ? 'Pipewright cannot confirm this command runs tests. Custom scripts are allowed, but verify it actually runs your test suite.'
          : 'Pipewright could not confirm the configured test command runs your test suite.'}
      </p>
    )
  }

  return null
}
