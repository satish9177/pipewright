import { useQuery } from '@tanstack/react-query'
import {
  llmApi,
  type LLMDiagnosticStatus,
  type LLMRoleDiagnostic,
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

/**
 * Read-only Provider Diagnostics panel (#33F).
 *
 * Displays the per-role provider/model availability from GET /llm/diagnostics.
 * It is observability ONLY: it has no editable settings, writes no .env, changes
 * no provider routing, and makes no LLM call. It self-fetches once and offers a
 * manual refresh — it does not auto-poll. It is visually secondary and must not
 * compete with execution controls or the Operator Attention Panel.
 */

const STATUS_META: Record<
  LLMDiagnosticStatus,
  { label: string; className: string }
> = {
  available: {
    label: 'Available',
    className: 'border-emerald-300 bg-emerald-100 text-emerald-800',
  },
  unavailable: {
    label: 'Unavailable',
    className: 'border-amber-300 bg-amber-100 text-amber-800',
  },
  blocked: {
    label: 'Blocked',
    className: 'border-red-300 bg-red-100 text-red-800',
  },
  unknown: {
    label: 'Unknown',
    className: 'border-slate-300 bg-slate-100 text-slate-600',
  },
}

const FAKE_BLOCKED_COPY =
  'Fake provider is blocked unless PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true is set ' +
  'for intentional local testing.'

function rowMessage(diag: LLMRoleDiagnostic): string | null {
  if (diag.fake_blocked) return FAKE_BLOCKED_COPY
  if (diag.status === 'available') return null
  return diag.message || null
}

function DiagnosticRow({ diag }: { diag: LLMRoleDiagnostic }) {
  const meta = STATUS_META[diag.status] ?? STATUS_META.unknown
  const message = rowMessage(diag)
  const providerModel = [diag.provider, diag.model].filter(Boolean).join(' · ')
  return (
    <li className="grid gap-1 rounded border border-slate-200 bg-white px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{diag.role}</span>
        <Badge variant="outline" className={meta.className}>
          {meta.label}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {providerModel || '—'}
        </span>
      </div>
      {message ? (
        <p className="text-xs text-slate-600">{message}</p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Provider and model configuration validated.
        </p>
      )}
    </li>
  )
}

export default function ProviderDiagnosticsPanel() {
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['llmDiagnostics'],
    queryFn: llmApi.diagnostics,
    // Read-only diagnostics: fetch on mount and on manual refresh only.
    refetchInterval: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm">Provider Diagnostics</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
        <CardDescription className="text-xs">
          Read-only — provider selection is currently configured through .env.
        </CardDescription>
      </CardHeader>
      <CardContent className="py-3 text-sm text-slate-800">
        {isLoading ? (
          <p className="text-xs text-muted-foreground">
            Loading provider diagnostics…
          </p>
        ) : isError ? (
          <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-800">
            Could not load provider diagnostics. The backend may be unavailable.
            This does not affect any run.
          </p>
        ) : !data || data.roles.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No provider roles to report.
          </p>
        ) : (
          <ul className="grid gap-1.5">
            {data.roles.map(diag => (
              <DiagnosticRow key={diag.role} diag={diag} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
