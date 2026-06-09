import type {
  MemoryTrustHealthState,
  MemoryTrustSummary,
} from '@/utils/memoryTrustSummary'

interface MemoryAttentionPanelProps {
  summary: MemoryTrustSummary
}

const STATUS_META: Record<
  MemoryTrustHealthState,
  { label: string; className: string }
> = {
  waiting: {
    label: 'Waiting on you',
    className: 'border-amber-200 bg-amber-100 text-amber-900',
  },
  needs_review: {
    label: 'Needs review',
    className: 'border-orange-200 bg-orange-100 text-orange-900',
  },
  healthy: {
    label: 'Looks healthy',
    className: 'border-emerald-200 bg-emerald-100 text-emerald-900',
  },
  unknown: {
    label: 'Needs review',
    className: 'border-slate-200 bg-slate-100 text-slate-800',
  },
}

function plural(count: number, singular: string, pluralLabel = `${singular}s`) {
  return `${count} ${count === 1 ? singular : pluralLabel}`
}

function memoryCount(count: number): string {
  return plural(count, 'memory', 'memories')
}

function memoryVerb(count: number, singular: string, pluralLabel: string) {
  return count === 1 ? singular : pluralLabel
}

function statusTitle(summary: MemoryTrustSummary): string {
  if (summary.healthState === 'waiting') {
    return 'Suggested memories are waiting for review'
  }
  if (summary.healthState === 'healthy') {
    return 'Project knowledge looks healthy'
  }
  if (summary.healthState === 'unknown') {
    return 'Memory state needs review'
  }
  if ((summary.possiblyOutdatedCount ?? 0) > 0) {
    return 'Some memories may be outdated'
  }
  return 'Memory state needs review'
}

function explanation(summary: MemoryTrustSummary): string {
  if (summary.healthState === 'healthy') {
    return 'Approved memory is in use, with no pending suggestions or possibly outdated items in the loaded data.'
  }
  if (summary.healthState === 'waiting') {
    return 'Pipewright has suggestions for you to inspect. They are background context only after you approve them.'
  }
  if (summary.healthState === 'unknown') {
    return 'Pipewright could not fully summarize memory state, so this panel does not treat the project as all clear.'
  }
  return 'Some project memory deserves a human look before you rely on it in future runs.'
}

function attentionItems(summary: MemoryTrustSummary): string[] {
  const items: string[] = []
  if (summary.pendingSuggestionCount == null) {
    items.push('Suggested memories could not be fully counted yet.')
  } else if (summary.pendingSuggestionCount > 0) {
    items.push(
      `${plural(summary.pendingSuggestionCount, 'suggested memory', 'suggested memories')} waiting. They are not used until you approve them.`,
    )
  }

  if (summary.possiblyOutdatedCount == null) {
    items.push('Possibly outdated memory could not be fully counted yet.')
  } else if (summary.possiblyOutdatedCount > 0) {
    items.push(
      `${memoryCount(summary.possiblyOutdatedCount)} ${memoryVerb(summary.possiblyOutdatedCount, 'is', 'are')} marked possibly outdated and not shown to the AI.`,
    )
  }

  if ((summary.unfamiliarStateCount ?? 0) > 0) {
    items.push(
      `${summary.unfamiliarStateCount} memory state ${memoryVerb(summary.unfamiliarStateCount ?? 0, 'is', 'are')} unfamiliar. Review below before assuming it is safe.`,
    )
  }

  if (summary.hasUnknownState) {
    items.push('Some memory state is loading, missing, or unfamiliar. Review below before assuming it is safe.')
  }

  if (items.length === 0) {
    items.push('No suggested, possibly outdated, retired, or replaced memory is visible in the loaded data.')
  }
  return items
}

function awarenessItems(summary: MemoryTrustSummary): string[] {
  const retiredOrReplaced = summary.retiredOrReplacedCount
  if (retiredOrReplaced == null) {
    return ['Retired and Replaced memory history could not be fully counted yet.']
  }
  if (retiredOrReplaced > 0) {
    return [
      `${memoryCount(retiredOrReplaced)} ${memoryVerb(retiredOrReplaced, 'is', 'are')} kept in history. ${retiredOrReplaced === 1 ? 'It is' : 'They are'} not used unless you start using ${retiredOrReplaced === 1 ? 'it' : 'them'} again.`,
    ]
  }
  return ['No Retired or Replaced memory is visible in the loaded data.']
}

function safeNextStep(summary: MemoryTrustSummary): string {
  if (summary.healthState === 'waiting') {
    return 'Review suggested memories below, but approve only what is true today.'
  }
  if ((summary.possiblyOutdatedCount ?? 0) > 0) {
    return 'Check possibly outdated memories before approving new ones.'
  }
  if (summary.healthState === 'healthy') {
    return 'Keep using the controls below when you need to add, verify, retire, or replace memory.'
  }
  return 'Review Memory Notes and Suggested memories below before trusting this summary.'
}

export default function MemoryAttentionPanel({
  summary,
}: MemoryAttentionPanelProps) {
  const status = STATUS_META[summary.healthState]

  return (
    <section
      className="rounded-xl border bg-card p-4 shadow-sm"
      aria-label="Project memory guidance"
    >
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div>
          <p className="font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Project knowledge
          </p>
          <h3 className="mt-1 text-lg font-semibold">{statusTitle(summary)}</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {explanation(summary)}
          </p>
        </div>
        <span
          className={`inline-flex w-fit items-center rounded-full border px-2.5 py-0.5 font-mono text-[10px] font-medium uppercase tracking-wider ${status.className}`}
        >
          {status.label}
        </span>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <div className="rounded-lg border bg-muted/30 p-3">
          <p className="text-xs font-semibold">What needs attention</p>
          <ul className="mt-2 grid gap-1.5 text-sm text-muted-foreground">
            {attentionItems(summary).map(item => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <p className="mt-3 text-xs font-semibold">Awareness</p>
          <ul className="mt-2 grid gap-1.5 text-sm text-muted-foreground">
            {awarenessItems(summary).map(item => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
        <div className="rounded-lg border bg-muted/30 p-3">
          <p className="text-xs font-semibold">Safe next step</p>
          <p className="mt-2 text-sm text-muted-foreground">
            {safeNextStep(summary)}
          </p>
        </div>
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-950">
          <p className="text-xs font-semibold">Do not do blindly</p>
          <p className="mt-2 text-sm">
            Do not approve suggestions just because they are new. Newer does not
            mean true. Memory is context for the AI, not a command.
          </p>
        </div>
      </div>
    </section>
  )
}
