import { useMemo } from 'react'

import type { TimelineEntry, TimelineLevel } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import type { RunViewMode } from '@/types/viewMode'

interface RunTimelineDetailProps {
  entry: TimelineEntry | null
  viewMode?: RunViewMode
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

const SECRET_PATTERNS = [
  /sk-[A-Za-z0-9_-]{8,}/g,
  /ghp_[A-Za-z0-9_]{8,}/g,
  /github_pat_[A-Za-z0-9_]{8,}/g,
  /AIza[0-9A-Za-z_-]{8,}/g,
  /xox[baprs]-[A-Za-z0-9-]{8,}/g,
  /AKIA[0-9A-Z]{8,}/g,
  /\bBearer\s+[A-Za-z0-9._-]{8,}/gi,
  /\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+/gi,
]

const SENSITIVE_KEY_PATTERN =
  /(diff|patch|stack|traceback|token|secret|password|api[_-]?key|authorization|auth|error|stderr|stdout|output|provider_error|git_error)/i

const TECHNICAL_PAYLOAD_MARKERS = [
  'traceback (most recent call last)',
  'diff --git',
  '\n@@ ',
  '\n+++ ',
  '\n--- ',
  'fatal:',
]

function labelFromToken(value: string): string {
  return value.replace(/[_-]/g, ' ')
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString([], {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function categoryClass(category: string): string {
  return CATEGORY_CLASS[category] ?? 'border-slate-200 bg-slate-50 text-slate-700'
}

function levelClass(level: TimelineLevel): string {
  return LEVEL_CLASS[level] ?? LEVEL_CLASS.info
}

function isApprovalClassEntry(entry: TimelineEntry): boolean {
  const approvalType = entry.data.approval_type
  return (
    entry.category === 'approval' ||
    entry.kind.includes('approval') ||
    typeof approvalType === 'string'
  )
}

function whyThisMatters(entry: TimelineEntry): string {
  if (entry.kind === 'approval_required') {
    return 'This marks a point where Pipewright is waiting for human review. The real approval controls live outside this read-only timeline.'
  }
  if (entry.kind === 'approval_decided') {
    return 'This records an approval decision that was already persisted. The timeline is an audit view and cannot change that decision.'
  }
  if (entry.kind.includes('scope_expansion')) {
    return 'This records a scope-expansion milestone. Scope is still granted only by the backend approval path, not by this display.'
  }
  if (entry.kind === 'memory_injected') {
    return 'This shows read-only memory provenance for the run. Memory is advisory and cannot grant scope, approvals, Git actions, or merge authority.'
  }
  if (entry.kind === 'git_pushed' || entry.kind === 'pr_created') {
    return 'This is a shipping milestone. It helps you audit what was pushed or opened, but it does not merge anything.'
  }

  switch (entry.category) {
    case 'lifecycle':
      return 'This anchors the run story: when the run started, changed state, or reached a terminal status.'
    case 'plan':
      return 'This records plan lineage. Plans describe intended work; approval and execution still happen through their own guarded controls.'
    case 'execution':
      return 'This tracks implementation progress. It is informational and does not expand scope or approve work.'
    case 'review':
      return 'This captures review or acknowledgement evidence. It is advisory/audit context, not an approval button.'
    case 'approval':
      return 'This is part of the human decision trail. The timeline shows what happened but never performs approval work.'
    case 'memory':
      return 'This records memory-related provenance. Memory remains advisory and cannot override source code or safety rules.'
    case 'ship':
      return 'This records shipping progress such as push or pull-request creation. Human review still owns the final merge decision.'
    default:
      return 'This is a persisted or live audit entry for the run. It is read-only context.'
  }
}

function safetyCopy(entry: TimelineEntry): string | null {
  if (isApprovalClassEntry(entry)) {
    return 'Approval-class entries are informational here. Use the existing guarded controls to approve or reject work. Pipewright never merges automatically.'
  }
  if (entry.category === 'ship') {
    return 'Shipping entries do not imply merge authority. Pipewright never merges automatically.'
  }
  return null
}

function sanitizeText(value: string): string {
  const lowered = value.toLowerCase()
  if (TECHNICAL_PAYLOAD_MARKERS.some(marker => lowered.includes(marker))) {
    return '[redacted technical payload]'
  }
  let sanitized = value
  for (const pattern of SECRET_PATTERNS) {
    sanitized = sanitized.replace(pattern, '[redacted]')
  }
  if (sanitized.length > 500) {
    return `${sanitized.slice(0, 497)}...`
  }
  return sanitized
}

function sanitizeTechnicalValue(value: unknown, key = ''): unknown {
  if (SENSITIVE_KEY_PATTERN.test(key)) return '[redacted]'
  if (typeof value === 'string') return sanitizeText(value)
  if (
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    value === null
  ) {
    return value
  }
  if (Array.isArray(value)) {
    return value.map(item => sanitizeTechnicalValue(item))
  }
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, childValue]) => [
        sanitizeText(childKey),
        sanitizeTechnicalValue(childValue, childKey),
      ]),
    )
  }
  return sanitizeText(String(value))
}

function technicalPayload(entry: TimelineEntry): Record<string, unknown> {
  return {
    id: entry.id,
    kind: entry.kind,
    stage: entry.stage,
    source: entry.source,
    data: sanitizeTechnicalValue(entry.data),
  }
}

export default function RunTimelineDetail({
  entry,
  viewMode = 'plain',
}: RunTimelineDetailProps) {
  const technicalJson = useMemo(
    () => (entry ? JSON.stringify(technicalPayload(entry), null, 2) : ''),
    [entry],
  )
  const isDeveloperMode = viewMode === 'developer'

  if (!entry) {
    return (
      <aside className="rounded border border-dashed bg-background px-4 py-6">
        <p className="text-sm font-medium text-foreground">
          Select a timeline entry
        </p>
        <p className="mt-1 text-sm text-muted-foreground">
          The detail panel will show plain-English context and sanitized
          technical fields for the selected row.
        </p>
      </aside>
    )
  }

  const safety = safetyCopy(entry)

  return (
    <aside className="grid gap-4 rounded border bg-background px-4 py-4">
      <div className="grid gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className={categoryClass(entry.category)}>
            {labelFromToken(entry.category)}
          </Badge>
          <Badge variant="outline" className={levelClass(entry.level)}>
            {entry.level}
          </Badge>
          {typeof entry.chunk_number === 'number' && (
            <Badge variant="outline">chunk {entry.chunk_number}</Badge>
          )}
          {isDeveloperMode && (
            <>
              <Badge variant="outline">{entry.source}</Badge>
              <Badge variant="outline">{entry.kind}</Badge>
              {entry.stage && <Badge variant="outline">{entry.stage}</Badge>}
            </>
          )}
        </div>
        <div>
          <p className="font-mono text-xs text-muted-foreground">
            {formatTimestamp(entry.ts)}
          </p>
          <h4 className="mt-1 text-base font-semibold leading-6">
            {entry.title}
          </h4>
          {isDeveloperMode && (
            <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
              {entry.id}
            </p>
          )}
        </div>
      </div>

      {entry.detail && (
        <div className="grid gap-1">
          <p className="text-xs font-medium uppercase text-muted-foreground">
            Detail
          </p>
          <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">
            {entry.detail}
          </p>
        </div>
      )}

      <div className="grid gap-1">
        <p className="text-xs font-medium uppercase text-muted-foreground">
          Why this matters
        </p>
        <p className="text-sm leading-6 text-muted-foreground">
          {whyThisMatters(entry)}
        </p>
      </div>

      {safety && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm leading-6 text-amber-900">
          {safety}
        </div>
      )}

      <details
        className="rounded border bg-muted/30 px-3 py-2"
        open={isDeveloperMode}
      >
        <summary className="cursor-pointer text-sm font-medium">
          Technical details
        </summary>
        <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-background px-3 py-2 font-mono text-xs text-muted-foreground">
          {technicalJson}
        </pre>
      </details>
    </aside>
  )
}
