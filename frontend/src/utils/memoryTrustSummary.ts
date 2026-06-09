import type { MemoryFact, MemorySuggestion } from '@/api/client'

export type MemoryTrustHealthState =
  | 'waiting'
  | 'needs_review'
  | 'healthy'
  | 'unknown'

export interface MemoryTrustSummary {
  activeCount: number | null
  pendingSuggestionCount: number | null
  possiblyOutdatedCount: number | null
  retiredCount: number | null
  replacedCount: number | null
  retiredOrReplacedCount: number | null
  unfamiliarStateCount: number | null
  reviewNeededCount: number | null
  hasUnknownState: boolean
  healthState: MemoryTrustHealthState
}

interface BuildMemoryTrustSummaryInput {
  facts?: MemoryFact[]
  suggestions?: MemorySuggestion[]
  factsLoading?: boolean
  suggestionsLoading?: boolean
  factsError?: boolean
  suggestionsError?: boolean
}

const KNOWN_MEMORY_STATUSES = new Set([
  'active',
  'stale',
  'archived',
  'historical',
])

const KNOWN_SUGGESTION_STATUSES = new Set([
  'pending',
  'approved',
  'rejected',
  'archived',
])

function countByStatus<T extends { status?: string }>(
  items: T[],
  status: string,
): number {
  return items.filter(item => item.status === status).length
}

function countUnfamiliarStatuses<T extends { status?: string }>(
  items: T[],
  knownStatuses: Set<string>,
): number {
  return items.filter(item => !item.status || !knownStatuses.has(item.status))
    .length
}

export function buildMemoryTrustSummary({
  facts,
  suggestions,
  factsLoading = false,
  suggestionsLoading = false,
  factsError = false,
  suggestionsError = false,
}: BuildMemoryTrustSummaryInput): MemoryTrustSummary {
  const dataMissing = !facts || !suggestions
  const hasUnknownMemoryStatus =
    facts?.some(fact => !KNOWN_MEMORY_STATUSES.has(fact.status)) ?? false
  const hasUnknownSuggestionStatus =
    suggestions?.some(
      suggestion => !KNOWN_SUGGESTION_STATUSES.has(suggestion.status),
    ) ?? false
  const hasUnknownState =
    dataMissing ||
    factsLoading ||
    suggestionsLoading ||
    factsError ||
    suggestionsError ||
    hasUnknownMemoryStatus ||
    hasUnknownSuggestionStatus

  if (hasUnknownState) {
    return {
      activeCount: facts ? countByStatus(facts, 'active') : null,
      pendingSuggestionCount: suggestions
        ? countByStatus(suggestions, 'pending')
        : null,
      possiblyOutdatedCount: facts ? countByStatus(facts, 'stale') : null,
      retiredCount: facts ? countByStatus(facts, 'archived') : null,
      replacedCount: facts ? countByStatus(facts, 'historical') : null,
      retiredOrReplacedCount: facts
        ? countByStatus(facts, 'archived') + countByStatus(facts, 'historical')
        : null,
      unfamiliarStateCount:
        facts && suggestions
          ? countUnfamiliarStatuses(facts, KNOWN_MEMORY_STATUSES) +
            countUnfamiliarStatuses(suggestions, KNOWN_SUGGESTION_STATUSES)
          : null,
      reviewNeededCount:
        facts && suggestions
          ? countByStatus(suggestions, 'pending') +
            countByStatus(facts, 'stale') +
            countUnfamiliarStatuses(facts, KNOWN_MEMORY_STATUSES) +
            countUnfamiliarStatuses(suggestions, KNOWN_SUGGESTION_STATUSES)
          : null,
      hasUnknownState: true,
      healthState: 'unknown',
    }
  }

  const activeCount = countByStatus(facts, 'active')
  const pendingSuggestionCount = countByStatus(suggestions, 'pending')
  const possiblyOutdatedCount = countByStatus(facts, 'stale')
  const retiredCount = countByStatus(facts, 'archived')
  const replacedCount = countByStatus(facts, 'historical')
  const retiredOrReplacedCount = retiredCount + replacedCount
  const unfamiliarStateCount = 0
  const reviewNeededCount =
    pendingSuggestionCount + possiblyOutdatedCount + unfamiliarStateCount

  let healthState: MemoryTrustHealthState = 'healthy'
  if (pendingSuggestionCount > 0) {
    healthState = 'waiting'
  } else if (possiblyOutdatedCount > 0) {
    healthState = 'needs_review'
  }

  return {
    activeCount,
    pendingSuggestionCount,
    possiblyOutdatedCount,
    retiredCount,
    replacedCount,
    retiredOrReplacedCount,
    unfamiliarStateCount,
    reviewNeededCount,
    hasUnknownState: false,
    healthState,
  }
}
