import type { Run } from '@/api/client'
import { Badge } from '@/components/ui/badge'
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

interface ReportViewProps {
  run: Run
  onBack: () => void
}

interface ReportFinding {
  title: string
  severity: string
  confidence: string
  file_path?: string | null
  line_hint?: string | null
  evidence?: string | null
  reasoning?: string | null
  suggested_next_action?: string | null
}

interface ReportResult {
  summary: string
  findings: ReportFinding[]
  limitations: string[]
  files_reviewed: string[]
  implementation_recommended: boolean
  next_action: string
  report_kind?: string | null
}

const READ_ONLY_BANNER =
  'Read-only report — no code was changed, no tests were run, and no commits or PRs were created.'

/**
 * Parse the structured report_json defensively. Any malformed / missing JSON
 * returns null so the caller falls back to plain_english_summary. Findings and
 * list fields are normalized so a partial payload still renders.
 */
function parseReport(raw: string | null | undefined): ReportResult | null {
  if (!raw || !raw.trim()) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null) return null

  const data = parsed as Record<string, unknown>
  const asStringList = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []

  const findings: ReportFinding[] = Array.isArray(data.findings)
    ? (data.findings as unknown[])
        .filter((f): f is Record<string, unknown> => typeof f === 'object' && f !== null)
        .map(f => ({
          title: typeof f.title === 'string' ? f.title : 'Untitled finding',
          severity: typeof f.severity === 'string' ? f.severity : 'info',
          confidence: typeof f.confidence === 'string' ? f.confidence : 'low',
          file_path: typeof f.file_path === 'string' ? f.file_path : null,
          line_hint: typeof f.line_hint === 'string' ? f.line_hint : null,
          evidence: typeof f.evidence === 'string' ? f.evidence : '',
          reasoning: typeof f.reasoning === 'string' ? f.reasoning : '',
          suggested_next_action:
            typeof f.suggested_next_action === 'string' ? f.suggested_next_action : '',
        }))
    : []

  return {
    summary: typeof data.summary === 'string' ? data.summary : '',
    findings,
    limitations: asStringList(data.limitations),
    files_reviewed: asStringList(data.files_reviewed),
    implementation_recommended: data.implementation_recommended === true,
    next_action: typeof data.next_action === 'string' ? data.next_action : '',
    report_kind: typeof data.report_kind === 'string' ? data.report_kind : null,
  }
}

function severityBadgeVariant(
  severity: string,
): 'destructive' | 'secondary' | 'outline' {
  const value = severity.toLowerCase()
  if (value === 'critical' || value === 'high') return 'destructive'
  if (value === 'medium' || value === 'low') return 'secondary'
  return 'outline'
}

function ReadOnlyBanner() {
  return (
    <div className="mb-6 rounded-md border border-muted-foreground/20 bg-muted px-4 py-3">
      <p className="text-sm text-muted-foreground">{READ_ONLY_BANNER}</p>
    </div>
  )
}

function RequestCard({ run }: { run: Run }) {
  return (
    <Card className="mb-6 border-muted-foreground/20">
      <CardHeader>
        <CardTitle className="text-base">Request</CardTitle>
        <CardDescription>
          This run was classified as read-only. No code changes, tests, Git
          operations, or pull requests were performed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground whitespace-pre-wrap">
          {run.feature_description}
        </p>
      </CardContent>
    </Card>
  )
}

function FindingCard({ finding }: { finding: ReportFinding }) {
  const location = [finding.file_path, finding.line_hint]
    .filter(Boolean)
    .join(':')
  return (
    <Card className="border-muted-foreground/20">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-sm">{finding.title}</CardTitle>
          <Badge variant={severityBadgeVariant(finding.severity)}>
            severity: {finding.severity}
          </Badge>
          <Badge variant="outline">confidence: {finding.confidence}</Badge>
        </div>
        {location && (
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            {location}
          </p>
        )}
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {finding.evidence && (
          <div>
            <p className="font-medium mb-1">Evidence</p>
            <p className="text-muted-foreground whitespace-pre-wrap">
              {finding.evidence}
            </p>
          </div>
        )}
        {finding.reasoning && (
          <div>
            <p className="font-medium mb-1">Reasoning</p>
            <p className="text-muted-foreground whitespace-pre-wrap">
              {finding.reasoning}
            </p>
          </div>
        )}
        {finding.suggested_next_action && (
          <div>
            <p className="font-medium mb-1">Suggested next action</p>
            <p className="text-muted-foreground whitespace-pre-wrap">
              {finding.suggested_next_action}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function StructuredReport({ report }: { report: ReportResult }) {
  return (
    <>
      {report.summary && (
        <Card className="mb-6 border-muted-foreground/20">
          <CardHeader>
            <CardTitle className="text-base">Summary</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground whitespace-pre-wrap">
              {report.summary}
            </p>
          </CardContent>
        </Card>
      )}

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="text-sm font-semibold">Findings</h3>
        </div>
        {report.findings.length > 0 ? (
          <div className="grid gap-3">
            {report.findings.map((finding, index) => (
              <FindingCard key={index} finding={finding} />
            ))}
          </div>
        ) : (
          <Card className="border-dashed">
            <CardContent className="py-6">
              <p className="text-sm text-muted-foreground">
                No findings were reported in the reviewed files.
              </p>
            </CardContent>
          </Card>
        )}
      </section>

      <Card className="mb-6 border-muted-foreground/20">
        <CardHeader>
          <CardTitle className="text-base">Files reviewed</CardTitle>
        </CardHeader>
        <CardContent>
          {report.files_reviewed.length > 0 ? (
            <ul className="list-disc pl-5 font-mono text-xs text-muted-foreground">
              {report.files_reviewed.map(path => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">
              No file contents were read for this analysis.
            </p>
          )}
        </CardContent>
      </Card>

      <Card className="mb-6 border-muted-foreground/20">
        <CardHeader>
          <CardTitle className="text-base">Limitations</CardTitle>
        </CardHeader>
        <CardContent>
          {report.limitations.length > 0 ? (
            <ul className="list-disc pl-5 text-sm text-muted-foreground">
              {report.limitations.map((limitation, index) => (
                <li key={index}>{limitation}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">
              None reported by the analyzer.
            </p>
          )}
        </CardContent>
      </Card>

      {report.next_action && (
        <Card className="mb-6 border-muted-foreground/20">
          <CardHeader>
            <CardTitle className="text-base">Next action</CardTitle>
            <CardDescription>
              Advisory only. This read-only report does not start or approve any
              implementation.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            <p className="text-sm text-muted-foreground whitespace-pre-wrap">
              {report.next_action}
            </p>
            <p className="text-xs text-muted-foreground">
              Implementation recommended:{' '}
              {report.implementation_recommended ? 'yes (advisory)' : 'no'}
            </p>
          </CardContent>
        </Card>
      )}
    </>
  )
}

function FallbackReport({ content }: { content: string }) {
  return (
    <Card className="mb-6 border-muted-foreground/20">
      <CardHeader>
        <CardTitle className="text-base">Report</CardTitle>
      </CardHeader>
      <CardContent>
        {content ? (
          <pre className="text-sm whitespace-pre-wrap font-mono bg-muted p-3 rounded">
            {content}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">
            No report content available for this run.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

export default function ReportView({ run, onBack }: ReportViewProps) {
  const report = parseReport(run.report_json)
  const fallbackContent = run.plain_english_summary?.trim() ?? ''

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Read-only Report</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {run.id}
          </p>
        </div>
        <RunStatusBadge status={run.status} />
      </div>

      <ReadOnlyBanner />

      <RequestCard run={run} />

      {report ? (
        <StructuredReport report={report} />
      ) : (
        <FallbackReport content={fallbackContent} />
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
