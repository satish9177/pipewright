import type { Run } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import RunStatusBadge from '@/components/RunStatusBadge'

interface ReportViewProps {
  run: Run
  onBack: () => void
}

export default function ReportView({ run, onBack }: ReportViewProps) {
  const reportContent = run.plain_english_summary?.trim() ?? ''

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

      <Card className="mb-6 border-muted-foreground/20">
        <CardHeader>
          <CardTitle className="text-base">Report</CardTitle>
        </CardHeader>
        <CardContent>
          {reportContent ? (
            <pre className="text-sm whitespace-pre-wrap font-mono bg-muted p-3 rounded">
              {reportContent}
            </pre>
          ) : (
            <p className="text-sm text-muted-foreground">
              No report content available for this run.
            </p>
          )}
          {run.created_at && (
            <p className="mt-3 text-xs text-muted-foreground">
              Created at {run.created_at}
            </p>
          )}
        </CardContent>
      </Card>

      <div className="mt-4">
        <Button variant="outline" size="sm" onClick={onBack}>
          Back
        </Button>
      </div>
    </div>
  )
}
