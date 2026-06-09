import { parseMemoryRealityConflict } from '@/utils/memoryReasonHumanize'

interface MemoryRealityWarningProps {
  memoryContent: string
  reason?: string | null
  status?: string | null
}

export default function MemoryRealityWarning({
  memoryContent,
  reason,
  status,
}: MemoryRealityWarningProps) {
  const conflict = parseMemoryRealityConflict(reason)
  if (!conflict) return null
  const statusCopy =
    status === 'stale'
      ? 'This memory is not shown to the AI while marked possibly outdated.'
      : 'This memory is not shown to the AI.'

  return (
    <div className="mt-3 grid gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
      <div>
        <p className="font-semibold">May not match the current repo</p>
        <p className="mt-1 text-xs">
          Pipewright found a signal that this memory may be outdated. The code
          wins; review before using this again.
        </p>
      </div>
      <dl className="grid gap-2 text-xs sm:grid-cols-[12rem_1fr]">
        <dt className="font-medium text-amber-900">Memory says</dt>
        <dd>{memoryContent || conflict.memoryLabel}</dd>
        <dt className="font-medium text-amber-900">
          Current repo appears to show
        </dt>
        <dd>{conflict.repoLabel}</dd>
        <dt className="font-medium text-amber-900">Why this matters</dt>
        <dd>
          A wrong memory can mislead future runs if you start using it again.
        </dd>
      </dl>
      <p className="rounded border border-amber-300 bg-amber-100 px-2 py-1 text-xs">
        {statusCopy}
      </p>
    </div>
  )
}
