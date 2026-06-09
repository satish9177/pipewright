const REASON_LABELS: Record<string, string> = {
  budget_dropped: 'Not enough room this run.',
  category_not_allowed_for_role: 'Not used by this role.',
  advisory_only: 'Read-only observation.',
  outdated: 'Outdated.',
  otudated: 'Outdated.',
}

const VALUE_LABELS: Record<string, string> = {
  mongo: 'MongoDB',
  mongodb: 'MongoDB',
  postgres: 'PostgreSQL',
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
  sqlite: 'SQLite',
}

function humanizeReasonValue(value: string): string {
  const normalized = value.trim()
  return VALUE_LABELS[normalized.toLowerCase()] ?? normalized
}

export interface MemoryRealityConflict {
  repoValue: string
  memoryValue: string
  repoLabel: string
  memoryLabel: string
}

export function parseMemoryRealityConflict(
  reason?: string | null,
): MemoryRealityConflict | null {
  if (!reason) return null
  const match = reason.match(
    /^(?:(?:was-)?repo reality conflict:|was-reality-conflict:)?\s*repo=([^,]+),\s*memory=(.+)$/i,
  )
  if (!match) return null
  const [, repoValue, memoryValue] = match
  return {
    repoValue: repoValue.trim(),
    memoryValue: memoryValue.trim(),
    repoLabel: humanizeReasonValue(repoValue),
    memoryLabel: humanizeReasonValue(memoryValue),
  }
}

function humanizeRealityConflict(reason: string): string | null {
  const conflict = parseMemoryRealityConflict(reason)
  if (!conflict) return null
  return (
    `The current repo appears to use ${conflict.repoLabel}, while ` +
    `this memory says ${conflict.memoryLabel}.`
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
