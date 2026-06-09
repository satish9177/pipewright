import type { MemoryTrustSummary } from '@/utils/memoryTrustSummary'

type ChipTone = 'ok' | 'warn' | 'alert' | 'neutral'

interface MemoryTrustStripProps {
  summary: MemoryTrustSummary
}

interface TrustChip {
  key: string
  label: string
  value: number | null
  unknownLabel?: string
  tone: ChipTone
  hint: string
}

const TONE_CLASS: Record<ChipTone, string> = {
  ok: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  warn: 'border-amber-300 bg-amber-50 text-amber-900',
  alert: 'border-red-300 bg-red-50 text-red-800',
  neutral: 'border-slate-200 bg-slate-100 text-slate-700',
}

function CountChip({ chip }: { chip: TrustChip }) {
  return (
    <div
      className={`rounded-lg border px-3 py-2 ${TONE_CLASS[chip.tone]}`}
      title={chip.hint}
    >
      <p className="text-[11px] font-medium uppercase tracking-wide opacity-80">
        {chip.label}
      </p>
      <p className="mt-1 text-lg font-semibold">
        {chip.value == null ? chip.unknownLabel ?? 'Unknown' : chip.value}
      </p>
    </div>
  )
}

export default function MemoryTrustStrip({ summary }: MemoryTrustStripProps) {
  const chips: TrustChip[] = [
    {
      key: 'active',
      label: 'In use',
      value: summary.activeCount,
      tone: summary.activeCount == null ? 'neutral' : 'ok',
      hint: 'Approved Memory Notes available as background context for future AI runs.',
    },
    {
      key: 'suggestions',
      label: 'Suggested memories',
      value: summary.pendingSuggestionCount,
      tone:
        summary.pendingSuggestionCount == null ||
        summary.pendingSuggestionCount > 0
          ? 'warn'
          : 'neutral',
      hint: 'Suggested memories are not used until you approve them.',
    },
    {
      key: 'outdated',
      label: 'Possibly outdated',
      value: summary.possiblyOutdatedCount,
      tone:
        summary.possiblyOutdatedCount == null ||
        summary.possiblyOutdatedCount > 0
          ? 'warn'
          : 'neutral',
      hint: 'Possibly outdated memories are not shown to the AI.',
    },
    {
      key: 'history',
      label: 'Retired / Replaced',
      value: summary.retiredOrReplacedCount,
      tone:
        summary.retiredOrReplacedCount == null ||
        summary.retiredOrReplacedCount > 0
          ? 'neutral'
          : 'ok',
      hint: 'Retired and Replaced memory is kept for history and review.',
    },
    {
      key: 'review',
      label: 'Review needed',
      value: summary.reviewNeededCount,
      unknownLabel: summary.hasUnknownState ? 'Needs review' : 'Unknown',
      tone:
        summary.reviewNeededCount == null || summary.reviewNeededCount > 0
          ? 'alert'
          : 'ok',
      hint: 'Fail-closed summary of memory that may need human review.',
    },
  ]

  return (
    <section aria-label="Project memory trust summary">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        {chips.map(chip => (
          <CountChip key={chip.key} chip={chip} />
        ))}
      </div>
    </section>
  )
}
