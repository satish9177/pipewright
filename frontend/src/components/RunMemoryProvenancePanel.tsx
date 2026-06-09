import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import {
  memoryApi,
  type MemoryInjectionAnalysisRef,
  type MemoryInjectionDuplicateCandidate,
  type MemoryInjectionEntry,
  type MemoryInjectionEvent,
  type MemoryInjectionRealityWarning,
  type MemoryInjectionSupersessionCandidate,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { humanizeMemoryReason } from '@/utils/memoryReasonHumanize'
import { getMemoryStatusDisplay } from '@/utils/memoryStatusDisplay'

interface RunMemoryProvenancePanelProps {
  runId: string
}

const ROLE_OPTIONS = ['all', 'triage', 'planner', 'coder']

function getErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } })
      .response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return fallback
}

function formatDate(value?: string | null): string {
  if (!value) return 'Not recorded'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

function formatChunk(chunkNumber?: number | null): string {
  return typeof chunkNumber === 'number' ? `chunk ${chunkNumber}` : 'run-level'
}

function shortId(value?: string | null): string {
  return value ? value.slice(0, 10) : 'none'
}

const SAFETY_CATEGORIES = new Set(['security', 'forbidden_paths'])

function exclusionReasonLabel(reason?: string | null): string {
  return humanizeMemoryReason(reason)
}

function isSafetyBudgetDrop(entry: MemoryInjectionEntry): boolean {
  return (
    entry.exclusion_reason === 'budget_dropped' &&
    !!entry.category &&
    SAFETY_CATEGORIES.has(entry.category)
  )
}

function summarizeExclusions(entries: MemoryInjectionEntry[]): string {
  const counts = new Map<string, number>()
  for (const entry of entries) {
    const reason = entry.exclusion_reason || 'other'
    counts.set(reason, (counts.get(reason) ?? 0) + 1)
  }
  const parts: string[] = []
  const budget = counts.get('budget_dropped')
  if (budget) parts.push(`${budget} not enough room this run`)
  const category = counts.get('category_not_allowed_for_role')
  if (category) parts.push(`${category} not used by this role`)
  for (const [reason, count] of counts) {
    if (
      reason !== 'budget_dropped' &&
      reason !== 'category_not_allowed_for_role'
    ) {
      parts.push(`${count} ${humanizeMemoryReason(reason)}`)
    }
  }
  return parts.join(', ')
}

function TraceRef({ refInfo }: { refInfo: MemoryInjectionAnalysisRef }) {
  const parts = [
    refInfo.role || 'unknown role',
    formatChunk(refInfo.chunk_number),
    refInfo.fact_id ? `memory ${shortId(refInfo.fact_id)}` : null,
  ].filter(Boolean)

  return (
    <div className="grid gap-1 rounded border border-slate-200 bg-white px-2 py-1.5">
      <p className="text-xs text-muted-foreground">{parts.join(' / ')}</p>
      <p className="text-sm leading-6 text-slate-800">{refInfo.content}</p>
    </div>
  )
}

function EntryRow({
  entry,
  included,
}: {
  entry: MemoryInjectionEntry
  included: boolean
}) {
  const statusDisplay = getMemoryStatusDisplay(entry.status_at_injection)

  return (
    <li className="grid gap-1">
      <p
        className={`text-sm leading-6 ${
          included ? 'text-slate-900' : 'text-muted-foreground line-through'
        }`}
      >
        <span className={included ? 'text-emerald-700' : 'text-amber-700'}>
          {included ? '+ ' : '- '}
        </span>
        {entry.content}
      </p>
      {!included && (
        <p className="pl-4 text-xs text-muted-foreground">
          {exclusionReasonLabel(entry.exclusion_reason)}
        </p>
      )}
      <details className="pl-4">
        <summary className="cursor-pointer text-[11px] text-muted-foreground">
          Technical details
        </summary>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {entry.status_at_injection && (
            <Badge
              variant="outline"
              className={statusDisplay.className}
              title={statusDisplay.tooltip}
            >
              {statusDisplay.label}
            </Badge>
          )}
          {entry.category && <Badge variant="outline">{entry.category}</Badge>}
          {entry.scope && <Badge variant="outline">{entry.scope}</Badge>}
          {typeof entry.priority === 'number' && (
            <span>priority {entry.priority}</span>
          )}
        </div>
      </details>
    </li>
  )
}

function EventCard({ event }: { event: MemoryInjectionEvent }) {
  const includedCount = event.included_count ?? event.included_entries.length
  const excludedCount = event.excluded_count ?? event.excluded_entries.length
  const roleLabel = event.role
    ? event.role.charAt(0).toUpperCase() + event.role.slice(1)
    : 'Unknown role'
  const safetyBudgetDrops = event.excluded_entries.filter(isSafetyBudgetDrop)
  const budgetDropped = event.excluded_entries.filter(
    entry => entry.exclusion_reason === 'budget_dropped',
  )

  return (
    <li className="grid gap-3 rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-950">
            {roleLabel}{' '}
            <span className="font-normal text-muted-foreground">
              received {includedCount}{' '}
              {includedCount === 1 ? 'memory' : 'memories'}
            </span>
          </p>
          <p className="text-xs text-muted-foreground">
            {formatChunk(event.chunk_number)} / attempt{' '}
            {event.attempt_number ?? 1} / {formatDate(event.created_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge variant="outline">{includedCount} used</Badge>
          {excludedCount > 0 && (
            <Badge variant="outline">{excludedCount} left out</Badge>
          )}
        </div>
      </div>

      {event.included_entries.length > 0 ? (
        <ul className="grid gap-2 border-l border-slate-200 pl-4">
          {event.included_entries.map((entry, index) => (
            <EntryRow
              key={`${event.id}-included-${entry.fact_id ?? index}`}
              entry={entry}
              included
            />
          ))}
        </ul>
      ) : (
        <p className="rounded border border-dashed px-3 py-2 text-sm text-muted-foreground">
          No memory was given to the AI for this event.
        </p>
      )}

      {event.excluded_entries.length > 0 && (
        <div className="grid gap-2">
          <p className="text-xs font-medium text-muted-foreground">
            Left out in this run
          </p>
          <p className="text-xs text-muted-foreground">
            {summarizeExclusions(event.excluded_entries)}
          </p>
          <ul className="grid gap-2 border-l border-slate-200 pl-4">
            {event.excluded_entries.map((entry, index) => (
              <EntryRow
                key={`${event.id}-excluded-${entry.fact_id ?? index}`}
                entry={entry}
                included={false}
              />
            ))}
          </ul>
        </div>
      )}

      {budgetDropped.length > 0 && (
        <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <span className="font-semibold">
            {budgetDropped.length}{' '}
            {budgetDropped.length === 1 ? 'memory was' : 'memories were'} left
            out to fit the prompt budget.
          </span>{' '}
          The AI did not see {budgetDropped.length === 1 ? 'it' : 'them'} in
          this role, so it could not act on{' '}
          {budgetDropped.length === 1 ? 'it' : 'them'}.
        </p>
      )}

      {safetyBudgetDrops.length > 0 && (
        <p className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm font-semibold text-red-700">
          Safety memory was left out for space this run.
        </p>
      )}

      <details className="rounded-lg bg-muted/30 px-3 py-2">
        <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
          Raw technical details
        </summary>
        <div className="mt-2 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
          <p>Memory space limit: {event.token_budget ?? 'not recorded'}</p>
          <p>
            Allowed memory types for this role:{' '}
            {event.category_policy.length > 0
              ? event.category_policy.join(', ')
              : 'not recorded'}
          </p>
          <p className="font-mono sm:col-span-2">
            Snapshot fingerprint: {event.entries_hash || 'not recorded'}
          </p>
          {(event.attempt_id || event.repo_head_sha) && (
            <p className="font-mono sm:col-span-2">
              attempt {shortId(event.attempt_id)} / repo{' '}
              {shortId(event.repo_head_sha)}
            </p>
          )}
        </div>
      </details>
    </li>
  )
}

function DuplicateCandidateCard({
  candidate,
}: {
  candidate: MemoryInjectionDuplicateCandidate
}) {
  return (
    <li className="grid gap-2 rounded-lg border border-amber-200 bg-amber-50/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="border-amber-300 bg-amber-100 text-amber-800">
          Possible duplicate
        </Badge>
        <Badge variant="outline">{candidate.relation}</Badge>
        <span className="text-xs text-muted-foreground">
          similarity {Math.round(candidate.similarity * 100)}%
        </span>
        {candidate.advisory_only && (
          <Badge variant="outline">Read-only observation</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {humanizeMemoryReason(candidate.reason)}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        <TraceRef refInfo={candidate.left} />
        <TraceRef refInfo={candidate.right} />
      </div>
    </li>
  )
}

function SupersessionCandidateCard({
  candidate,
}: {
  candidate: MemoryInjectionSupersessionCandidate
}) {
  return (
    <li className="grid gap-2 rounded-lg border border-blue-200 bg-blue-50/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="border-blue-300 bg-blue-100 text-blue-800">
          Possible replacement candidate
        </Badge>
        <Badge variant="outline">{candidate.relation}</Badge>
        <Badge variant="outline">{candidate.dimension}</Badge>
        {candidate.advisory_only && (
          <Badge variant="outline">Read-only observation</Badge>
        )}
      </div>
      <p className="text-xs font-medium text-blue-800">
        Newer does not mean true. You decide which memory to use.
      </p>
      <p className="text-xs text-muted-foreground">
        {humanizeMemoryReason(candidate.reason)}
      </p>
      <p className="text-xs text-muted-foreground">
        Values: {candidate.left_value} / {candidate.right_value}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        <TraceRef refInfo={candidate.left} />
        <TraceRef refInfo={candidate.right} />
      </div>
    </li>
  )
}

function RealityWarningCard({
  warning,
}: {
  warning: MemoryInjectionRealityWarning
}) {
  const trace = [
    warning.role || 'unknown role',
    formatChunk(warning.chunk_number),
    warning.fact_id ? `memory ${shortId(warning.fact_id)}` : null,
  ]
    .filter(Boolean)
    .join(' / ')

  return (
    <li className="grid gap-2 rounded-lg border border-orange-200 bg-orange-50/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          className="border-orange-300 bg-orange-100 text-orange-800"
        >
          Code/memory mismatch warning
        </Badge>
        <Badge variant="outline">{warning.dimension}</Badge>
        {warning.advisory_only && (
          <Badge variant="outline">Read-only observation</Badge>
        )}
      </div>
      <p className="text-xs font-medium text-orange-800">
        Repo signal suggests this memory may be outdated. If memory and current
        code disagree, code wins.
      </p>
      <p className="text-sm leading-6 text-slate-800">{warning.memory_content}</p>
      <p className="text-xs text-muted-foreground">
        Memory value: {warning.memory_value ?? 'unknown'} / repo signal:{' '}
        {warning.repo_value ?? 'unknown'}
      </p>
      {trace && <p className="text-xs text-muted-foreground">{trace}</p>}
    </li>
  )
}

export default function RunMemoryProvenancePanel({
  runId,
}: RunMemoryProvenancePanelProps) {
  const [expanded, setExpanded] = useState(false)
  const [roleFilter, setRoleFilter] = useState('all')
  const [chunkInput, setChunkInput] = useState('')

  const trimmedChunkInput = chunkInput.trim()
  const chunkNumber =
    trimmedChunkInput === '' ? undefined : Number(trimmedChunkInput)
  const chunkFilterIsValid =
    chunkNumber === undefined ||
    (Number.isInteger(chunkNumber) && chunkNumber >= 0)

  const filters = useMemo(() => {
    return {
      ...(roleFilter === 'all' ? {} : { role: roleFilter }),
      ...(chunkNumber === undefined ? {} : { chunk_number: chunkNumber }),
    }
  }, [chunkNumber, roleFilter])

  const provenanceQuery = useQuery({
    queryKey: ['run-memory-injections', runId, filters],
    queryFn: () => memoryApi.getRunMemoryInjections(runId, filters),
    enabled: expanded && chunkFilterIsValid,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const analysisQuery = useQuery({
    queryKey: ['run-memory-injection-analysis', runId, filters],
    queryFn: () => memoryApi.getRunMemoryInjectionAnalysis(runId, filters),
    enabled: expanded && chunkFilterIsValid,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const events = provenanceQuery.data?.events ?? []
  const analysis = analysisQuery.data?.analysis
  const includedTotal =
    analysis?.total_included_entries ??
    events.reduce(
      (total, event) =>
        total + (event.included_count ?? event.included_entries.length),
      0,
    )
  const duplicateCount = analysis?.duplicate_candidate_count ?? 0
  const supersessionCount = analysis?.supersession_candidate_count ?? 0
  const isLoading =
    (provenanceQuery.isLoading || analysisQuery.isLoading) && expanded
  const isRefreshing = provenanceQuery.isFetching || analysisQuery.isFetching
  const error =
    provenanceQuery.error || analysisQuery.error
      ? getErrorMessage(
          provenanceQuery.error || analysisQuery.error,
          'Failed to load what the AI was given.',
        )
      : null

  function refresh() {
    provenanceQuery.refetch()
    analysisQuery.refetch()
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
          <div>
            <p className="font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Recent AI behavior
            </p>
            <CardTitle className="mt-1 text-base">
              What Pipewright told the AI
            </CardTitle>
            <CardDescription>
              Read-only view of the memories Pipewright shared with each role.
              Viewing it never changes memory or the run.
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setExpanded(previous => !previous)}
            aria-expanded={expanded}
            className="w-fit"
          >
            {expanded ? 'Hide details' : 'Load what the AI was given'}
          </Button>
        </div>
      </CardHeader>

      {expanded && (
        <CardContent className="grid gap-4">
          <div className="grid gap-3 rounded-lg border bg-muted/30 p-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <div className="grid gap-2">
              <Label htmlFor="memory-provenance-role">Role</Label>
              <select
                id="memory-provenance-role"
                value={roleFilter}
                onChange={event => setRoleFilter(event.target.value)}
                className="h-8 w-full rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {ROLE_OPTIONS.map(role => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="memory-provenance-chunk">Chunk</Label>
              <Input
                id="memory-provenance-chunk"
                type="number"
                min={0}
                value={chunkInput}
                onChange={event => setChunkInput(event.target.value)}
                placeholder="all"
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={refresh}
              disabled={isRefreshing || !chunkFilterIsValid}
            >
              {isRefreshing ? 'Refreshing...' : 'Refresh'}
            </Button>
          </div>

          {!chunkFilterIsValid && (
            <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              Enter a whole chunk number or leave the chunk filter blank.
            </p>
          )}

          {isLoading && (
            <p className="text-sm text-muted-foreground">
              Loading what the AI was given...
            </p>
          )}

          {error && (
            <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
              {error}
            </p>
          )}

          {!isLoading && !error && chunkFilterIsValid && (
            <>
              <div className="grid gap-2 sm:grid-cols-4">
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">Events</p>
                  <p className="text-lg font-semibold">{events.length}</p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">Given to AI</p>
                  <p className="text-lg font-semibold">{includedTotal}</p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">
                    Duplicate candidates
                  </p>
                  <p className="text-lg font-semibold">{duplicateCount}</p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">
                    Replacement candidates
                  </p>
                  <p className="text-lg font-semibold">{supersessionCount}</p>
                </div>
              </div>

              {events.length === 0 ? (
                <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                  No memory-use snapshot recorded for this run. Older runs or
                  runs without memory may show no entries.
                </p>
              ) : (
                <div className="grid gap-3">
                  <div>
                    <h4 className="text-sm font-semibold">
                      Role-by-role memory
                    </h4>
                    <p className="text-xs text-muted-foreground">
                      Included and left-out memories from the stored run
                      snapshot.
                    </p>
                  </div>
                  <ul className="grid gap-3">
                    {events.map(event => (
                      <EventCard key={event.id} event={event} />
                    ))}
                  </ul>
                </div>
              )}

              {analysis && (
                <div className="grid gap-3 rounded-lg border border-indigo-200 bg-indigo-50/40 p-3">
                  <div>
                    <h4 className="text-sm font-semibold">
                      Advisory observations
                    </h4>
                    <p className="text-xs text-muted-foreground">
                      These are read-only observations from the stored memory
                      snapshot. The system did not change memory.
                    </p>
                  </div>

                  {analysis.warnings.length > 0 && (
                    <ul className="grid gap-1">
                      {analysis.warnings.map((warning, index) => (
                        <li
                          key={index}
                          className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-800"
                        >
                          {warning}
                        </li>
                      ))}
                    </ul>
                  )}

                  {analysis.duplicate_candidates.length === 0 &&
                  analysis.supersession_candidates.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      No advisory duplicate or replacement candidates were found
                      in this memory snapshot.
                    </p>
                  ) : (
                    <div className="grid gap-3">
                      {analysis.duplicate_candidates.length > 0 && (
                        <div className="grid gap-2">
                          <p className="text-xs font-medium text-slate-600">
                            Possible duplicates
                          </p>
                          <ul className="grid gap-2">
                            {analysis.duplicate_candidates.map((candidate, index) => (
                              <DuplicateCandidateCard
                                key={`${candidate.left.event_id}-${candidate.right.event_id}-${index}`}
                                candidate={candidate}
                              />
                            ))}
                          </ul>
                        </div>
                      )}

                      {analysis.supersession_candidates.length > 0 && (
                        <div className="grid gap-2">
                          <p className="text-xs font-medium text-slate-600">
                            Possible replacement candidates
                          </p>
                          <ul className="grid gap-2">
                            {analysis.supersession_candidates.map((candidate, index) => (
                              <SupersessionCandidateCard
                                key={`${candidate.left.event_id}-${candidate.right.event_id}-${index}`}
                                candidate={candidate}
                              />
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}

                  <div className="grid gap-2">
                    <div>
                      <p className="text-xs font-medium text-slate-600">
                        Repo reality warnings
                      </p>
                      <p className="text-xs text-muted-foreground">
                        These are read-only warnings. The system did not change
                        memory.
                      </p>
                    </div>
                    {(analysis.reality_warnings ?? []).length > 0 ? (
                      <ul className="grid gap-2">
                        {(analysis.reality_warnings ?? []).map((warning, index) => (
                          <RealityWarningCard
                            key={`${warning.event_id ?? 'event'}-${warning.fact_id ?? 'fact'}-${warning.dimension}-${index}`}
                            warning={warning}
                          />
                        ))}
                      </ul>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        {analysis.reality_signal_available
                          ? 'No repo reality mismatches were found for the memory given to the AI.'
                          : 'Repo reality signals are unavailable for this run; no reality checks were run.'}
                      </p>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}
