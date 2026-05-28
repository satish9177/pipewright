import type { ChunkDefinition, ChunkPlanResponse, Run } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import RunStatusBadge from '@/components/RunStatusBadge'

interface PlanViewProps {
  run: Run
  chunkPlan: ChunkPlanResponse | null | undefined
  onBack: () => void
  onStartImplementation?: () => void
  isStartingImplementation?: boolean
  startImplementationError?: string | null
}

function riskClassName(risk: string) {
  if (risk === 'high') return 'text-red-600'
  if (risk === 'medium') return 'text-yellow-600'
  return 'text-green-600'
}

function ChunkCard({ chunk }: { chunk: ChunkDefinition }) {
  return (
    <Card className="border-muted-foreground/20">
      <CardHeader>
        <CardTitle className="text-sm">
          Planned chunk {chunk.chunk_number}: {chunk.title}
        </CardTitle>
        <CardDescription>{chunk.description}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <p className="font-medium">Risk</p>
            <p className={riskClassName(chunk.risk_level)}>
              {chunk.risk_level}
            </p>
          </div>
          <div>
            <p className="font-medium">Human review</p>
            <p className="text-muted-foreground">
              {chunk.requires_human_review ? 'required' : 'not required'}
            </p>
          </div>
          <div>
            <p className="font-medium">Token estimate</p>
            <p className="text-muted-foreground">{chunk.token_estimate}</p>
          </div>
        </div>

        <div>
          <p className="font-medium mb-1">Files expected</p>
          {chunk.files_expected.length > 0 ? (
            <ul className="text-muted-foreground font-mono text-xs list-disc pl-5">
              {chunk.files_expected.map(path => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground">none</p>
          )}
        </div>

        <div>
          <p className="font-medium mb-1">Depends on</p>
          <p className="text-muted-foreground">
            {chunk.depends_on.length > 0
              ? chunk.depends_on.join(', ')
              : 'no dependencies'}
          </p>
        </div>

        {chunk.rationale && (
          <div>
            <p className="font-medium mb-1">Rationale</p>
            <p className="text-muted-foreground whitespace-pre-wrap">
              {chunk.rationale}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export default function PlanView({
  run,
  chunkPlan,
  onBack,
  onStartImplementation,
  isStartingImplementation = false,
  startImplementationError = null,
}: PlanViewProps) {
  const triage = chunkPlan?.triage ?? null
  const reasoning = run.plain_english_summary?.trim() ?? ''
  const chunks = triage?.chunks ?? []
  const showHandoff = run.status === 'plan_ready' && !!onStartImplementation

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Plan Ready</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {run.id}
          </p>
        </div>
        <RunStatusBadge status={run.status} />
      </div>

      <Card className="mb-6 border-muted-foreground/20">
        <CardHeader>
          <CardTitle className="text-base">Request</CardTitle>
          <CardDescription>
            This run is read-only. No code was changed, no tests were run, and
            no commits were created.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div>
            <p className="text-sm font-medium mb-1">Feature</p>
            <p className="text-sm text-muted-foreground whitespace-pre-wrap">
              {run.feature_description}
            </p>
          </div>
          {triage?.complexity && (
            <div>
              <p className="text-sm font-medium mb-1">Complexity</p>
              <p className="text-sm text-muted-foreground">
                {triage.complexity}
              </p>
            </div>
          )}
          {reasoning && (
            <div>
              <p className="text-sm font-medium mb-1">Reasoning</p>
              <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                {reasoning}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="text-sm font-semibold">Planned chunks</h3>
          <p className="text-xs text-muted-foreground">
            Proposed breakdown only. Plan-only runs cannot be executed or
            approved.
          </p>
        </div>

        {chunks.length > 0 ? (
          <div className="grid gap-3">
            {chunks.map(chunk => (
              <ChunkCard key={chunk.chunk_number} chunk={chunk} />
            ))}
          </div>
        ) : (
          <Card className="border-dashed">
            <CardContent className="py-6">
              <p className="text-sm font-medium">No planned chunks available</p>
              <p className="mt-1 text-sm text-muted-foreground">
                The plan was not stored for this run.
              </p>
            </CardContent>
          </Card>
        )}
      </section>

      {showHandoff && (
        <Card className="mb-6 border-blue-400/40">
          <CardHeader>
            <CardTitle className="text-base">Next step</CardTitle>
            <CardDescription>
              This creates a new implementation run using this plan as context.
              The original plan remains read-only. Normal risk, scope, test,
              and approval gates still apply.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div>
              <Button
                onClick={() => onStartImplementation?.()}
                disabled={isStartingImplementation}
              >
                {isStartingImplementation
                  ? 'Starting implementation...'
                  : 'Start implementation from this plan'}
              </Button>
            </div>
            {startImplementationError && (
              <p className="text-sm text-red-500">{startImplementationError}</p>
            )}
          </CardContent>
        </Card>
      )}

      {run.created_at && (
        <>
          <Separator className="my-4" />
          <p className="text-xs text-muted-foreground">
            Created at {run.created_at}
          </p>
        </>
      )}

      <div className="mt-4">
        <Button variant="outline" size="sm" onClick={onBack}>
          Back
        </Button>
      </div>
    </div>
  )
}
