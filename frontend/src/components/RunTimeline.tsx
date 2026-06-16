import { useMemo, useState } from 'react'

import type {
  TimelineCategory,
  TimelineEntry,
  TimelineLevel,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import RunTimelineDetail from '@/components/RunTimelineDetail'
import { cn } from '@/lib/utils'
import type { RunEvent } from '@/types/events'

interface RunTimelineProps {
  entries?: TimelineEntry[]
  liveEvents?: RunEvent[]
  isLoading?: boolean
  error?: unknown
}

const CATEGORY_ORDER: Record<string, number> = {
  lifecycle: 0,
  plan: 10,
  execution: 20,
  review: 30,
  approval: 40,
  memory: 50,
  ship: 60,
}

const CATEGORY_CLASS: Record<string, string> = {
  lifecycle: 'border-slate-200 bg-slate-50 text-slate-700',
  plan: 'border-blue-200 bg-blue-50 text-blue-700',
  execution: 'border-amber-200 bg-amber-50 text-amber-800',
  review: 'border-violet-200 bg-violet-50 text-violet-700',
  approval: 'border-rose-200 bg-rose-50 text-rose-700',
  memory: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  ship: 'border-cyan-200 bg-cyan-50 text-cyan-700',
}

const LEVEL_CLASS: Record<TimelineLevel, string> = {
  info: 'border-slate-200 bg-background text-muted-foreground',
  warn: 'border-amber-300 bg-amber-50 text-amber-800',
  error: 'border-red-300 bg-red-50 text-red-700',
}

function getErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } })
      .response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return 'Timeline is unavailable right now.'
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function labelFromToken(value: string): string {
  return value.replace(/[_-]/g, ' ')
}

function liveEventCategory(event: RunEvent): TimelineCategory {
  const kind = event.kind.toLowerCase()
  const stage = event.stage?.toLowerCase() ?? ''
  if (kind.includes('github') || kind.includes('git') || stage === 'github') {
    return 'ship'
  }
  if (kind.includes('approval') || stage === 'approval') {
    return 'approval'
  }
  if (kind.includes('review')) {
    return 'review'
  }
  if (kind.includes('memory')) {
    return 'memory'
  }
  if (kind.includes('plan') || stage === 'planner') {
    return 'plan'
  }
  if (kind.includes('run_status') || kind === 'terminal' || stage === 'triage') {
    return 'lifecycle'
  }
  return 'execution'
}

function titleForLiveEvent(event: RunEvent): string {
  if (event.message) return event.message
  return labelFromToken(event.kind)
}

function liveEventToTimelineEntry(event: RunEvent): TimelineEntry {
  return {
    id: event.id,
    ts: event.ts,
    kind: event.kind,
    stage: event.stage,
    chunk_number: event.chunk_number,
    level: event.level,
    category: liveEventCategory(event),
    title: titleForLiveEvent(event),
    detail: event.stage
      ? `${labelFromToken(event.stage)} / ${labelFromToken(event.kind)}`
      : labelFromToken(event.kind),
    source: 'live',
    data: event.data,
  }
}

function timelineSortKey(entry: TimelineEntry): [string, number, string, string] {
  return [
    entry.ts,
    CATEGORY_ORDER[entry.category] ?? 999,
    entry.kind,
    entry.id,
  ]
}

function compareTimelineEntries(a: TimelineEntry, b: TimelineEntry): number {
  const aKey = timelineSortKey(a)
  const bKey = timelineSortKey(b)
  for (let index = 0; index < aKey.length; index += 1) {
    if (aKey[index] < bKey[index]) return -1
    if (aKey[index] > bKey[index]) return 1
  }
  return 0
}

function mergeTimelineEntries(
  persistedEntries: TimelineEntry[] = [],
  liveEvents: RunEvent[] = [],
): TimelineEntry[] {
  const byId = new Map<string, TimelineEntry>()
  for (const entry of persistedEntries) {
    byId.set(entry.id, entry)
  }
  for (const event of liveEvents) {
    if (event.kind === 'heartbeat' || byId.has(event.id)) continue
    byId.set(event.id, liveEventToTimelineEntry(event))
  }
  return [...byId.values()].sort(compareTimelineEntries)
}

function categoryClass(category: string): string {
  return CATEGORY_CLASS[category] ?? 'border-slate-200 bg-slate-50 text-slate-700'
}

function levelClass(level: TimelineLevel): string {
  return LEVEL_CLASS[level] ?? LEVEL_CLASS.info
}

function EmptyState() {
  return (
    <div className="rounded border border-dashed px-4 py-8 text-center">
      <p className="font-medium text-foreground">No timeline entries yet</p>
      <p className="mt-1 text-sm text-muted-foreground">
        Persisted run milestones will appear here when the backend records them.
      </p>
    </div>
  )
}

export default function RunTimeline({
  entries = [],
  liveEvents = [],
  isLoading = false,
  error,
}: RunTimelineProps) {
  const timelineEntries = useMemo(
    () => mergeTimelineEntries(entries, liveEvents),
    [entries, liveEvents],
  )
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const activeSelectedId =
    selectedId && timelineEntries.some(entry => entry.id === selectedId)
      ? selectedId
      : null
  const selectedEntry =
    timelineEntries.find(entry => entry.id === activeSelectedId) ?? null

  if (isLoading) {
    return (
      <div className="rounded border px-4 py-8 text-center">
        <p className="font-medium text-foreground">Loading timeline...</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Reading persisted run milestones.
        </p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {getErrorMessage(error)}
      </div>
    )
  }

  if (timelineEntries.length === 0) return <EmptyState />

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">
          {timelineEntries.length}{' '}
          {timelineEntries.length === 1 ? 'entry' : 'entries'}
        </p>
        <p className="text-xs text-muted-foreground">
          Persisted backbone plus live tail, deduped by event id.
        </p>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,380px)]">
        <div className="max-h-96 overflow-y-auto rounded border bg-background">
          <div className="divide-y">
            {timelineEntries.map(entry => {
              const selected = activeSelectedId === entry.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => setSelectedId(entry.id)}
                  className={cn(
                    'grid w-full gap-2 px-3 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    selected && 'bg-muted ring-1 ring-ring',
                  )}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-muted-foreground">
                      {formatTimestamp(entry.ts)}
                    </span>
                    <Badge
                      variant="outline"
                      className={categoryClass(entry.category)}
                    >
                      {labelFromToken(entry.category)}
                    </Badge>
                    <Badge variant="outline" className={levelClass(entry.level)}>
                      {entry.level}
                    </Badge>
                    {typeof entry.chunk_number === 'number' && (
                      <Badge variant="outline">
                        chunk {entry.chunk_number}
                      </Badge>
                    )}
                    <Badge variant="outline">{entry.source}</Badge>
                  </div>
                  <div className="grid gap-1">
                    <p className="text-sm font-medium leading-6 text-foreground">
                      {entry.title}
                    </p>
                    {entry.detail && (
                      <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">
                        {entry.detail}
                      </p>
                    )}
                  </div>
                </button>
              )
            })}
          </div>
        </div>
        <RunTimelineDetail entry={selectedEntry} />
      </div>
    </div>
  )
}
