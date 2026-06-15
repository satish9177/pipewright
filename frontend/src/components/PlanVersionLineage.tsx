import type {
  ChunkPlanStatus,
  PlanVersionEntry,
  PlanVersionsResponse,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'

interface PlanVersionLineageProps {
  // Page-owned lineage from GET /runs/{run_id}/plan-versions. Optional/null-safe:
  // while the query is loading/disabled or errored, this is null and the
  // component renders nothing.
  lineage?: PlanVersionsResponse | null
  // The live chunk-plan status (already held by ChunkPlanPanel). Used ONLY to
  // interpret a null approved_version honestly: awaiting => "not yet approved"
  // (no badge); approved => "version not recorded" for legacy runs.
  chunkPlanStatus?: ChunkPlanStatus | null
}

// Slice C: read-only plan-version lineage display. This is provenance/audit only.
// It owns no mutations and no controls; it must never disable, gate, or block
// approve / reject / execute, and it never compares the live plan to a stamped
// version (no mismatch warning). All server strings — including the sanitized
// plan-turn message — are rendered as plain React children; never via
// dangerouslySetInnerHTML.

// initial/seeded versions are the original plan; plan_turn versions are
// revisions. Unknown future sources degrade to a neutral label, never an error.
function sourceLabel(source: string): string {
  if (source === 'initial' || source === 'seeded') return 'original plan'
  if (source === 'plan_turn') return 'revision'
  return source
}

function formatTimestamp(value?: string | null): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

// A single version row inside the expanded "Plan history" disclosure. plan-turn
// rows surface the revision number, created_at, and the sanitized message; the
// row matching approved_version carries an "Approved: vN" badge.
function VersionRow({
  entry,
  isApproved,
  isCurrent,
}: {
  entry: PlanVersionEntry
  isApproved: boolean
  isCurrent: boolean
}) {
  const turn = entry.created_from_turn
  const created = formatTimestamp(entry.created_at)
  return (
    <li className="grid gap-1 rounded border bg-background px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">
          v{entry.version} · {sourceLabel(entry.source)}
          {turn ? ` ${turn.turn_number}` : ''}
        </span>
        {isCurrent && (
          <Badge
            variant="outline"
            className="border-slate-300 bg-slate-100 text-slate-700"
          >
            Current
          </Badge>
        )}
        {isApproved && (
          <Badge
            variant="outline"
            className="border-emerald-300 bg-emerald-100 text-emerald-800"
          >
            Approved: v{entry.version}
          </Badge>
        )}
        {created && (
          <span className="text-xs text-muted-foreground">{created}</span>
        )}
      </div>
      {turn?.message && (
        <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {turn.message}
        </p>
      )}
    </li>
  )
}

export default function PlanVersionLineage({
  lineage,
  chunkPlanStatus,
}: PlanVersionLineageProps) {
  const versions = lineage?.versions ?? []
  const approvedVersion = lineage?.approved_version ?? null
  // chunk_plan_status stays `approved` through running/completion/final, so this
  // covers "already approved/completed".
  const planApproved = chunkPlanStatus === 'approved'

  // Zero recorded versions: only the approved-legacy case says anything; an
  // unapproved run with no lineage stays silent (never a fabricated v1).
  if (versions.length === 0) {
    if (planApproved && approvedVersion === null) {
      return (
        <p className="text-xs text-muted-foreground">
          Approved · version history not recorded
        </p>
      )
    }
    return null
  }

  const ordered = [...versions].sort((a, b) => a.version - b.version)
  const currentVersion = ordered[ordered.length - 1].version

  // v1-only: a quiet inline line, no disclosure. Use the lone version's own
  // source label (original plan for initial/seeded).
  if (ordered.length === 1) {
    const only = ordered[0]
    return (
      <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>
          Version {only.version} · {sourceLabel(only.source)}
        </span>
        {approvedVersion === only.version && (
          <Badge
            variant="outline"
            className="border-emerald-300 bg-emerald-100 text-emerald-800"
          >
            Approved: v{only.version}
          </Badge>
        )}
      </p>
    )
  }

  // v2/v3+: collapsed-by-default disclosure. The header states the current
  // version and, when known, the approved version. A null approved_version while
  // awaiting approval shows nothing (never "approved unknown"); a null
  // approved_version on an already-approved run reads "version not recorded".
  const approvedSummary =
    approvedVersion !== null
      ? `approved v${approvedVersion}`
      : planApproved
        ? 'approved · version not recorded'
        : null

  return (
    <details className="grid gap-2 rounded border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
      <summary className="cursor-pointer font-medium text-foreground">
        Plan history · v{currentVersion} current
        {approvedSummary ? ` · ${approvedSummary}` : ''}
      </summary>
      <ul className="mt-2 grid gap-2">
        {ordered.map(entry => (
          <VersionRow
            key={entry.version}
            entry={entry}
            isApproved={approvedVersion === entry.version}
            isCurrent={entry.version === currentVersion}
          />
        ))}
      </ul>
    </details>
  )
}
