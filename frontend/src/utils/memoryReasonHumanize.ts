const REASON_LABELS: Record<string, string> = {
  budget_dropped: 'Not enough room this run.',
  category_not_allowed_for_role: 'Not used by this role.',
  advisory_only: 'Read-only observation.',
}

function humanizeRealityConflict(reason: string): string | null {
  const match = reason.match(/^was-reality-conflict:\s*repo=([^,]+),\s*memory=(.+)$/)
  if (!match) return null
  const [, repoValue, memoryValue] = match
  return (
    `The current repo appears to use ${repoValue}, while this memory says ` +
    `${memoryValue}.`
  )
}

export function humanizeMemoryReason(reason?: string | null): string {
  if (!reason) return 'Not used in this run.'
  const direct = REASON_LABELS[reason]
  if (direct) return direct
  const realityConflict = humanizeRealityConflict(reason)
  if (realityConflict) return realityConflict
  return reason
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^./, first => first.toUpperCase())
}

export function humanizeMemoryReasons(reasons: string[]): string[] {
  return reasons.map(humanizeMemoryReason)
}
