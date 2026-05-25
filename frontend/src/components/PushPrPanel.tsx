import type { Project, Run } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

interface PushPrPanelProps {
  run: Run
  project?: Project
  isPushing: boolean
  message: string | null
  error: string | null
  onPush: () => void
}

function hasGithubConfigWarning(project?: Project) {
  if (!project) return false
  return (
    !project.has_github_token ||
    !project.github_owner ||
    !project.github_repo
  )
}

export default function PushPrPanel({
  run,
  project,
  isPushing,
  message,
  error,
  onPush,
}: PushPrPanelProps) {
  const hasPr = Boolean(run.pr_url)
  const canPush =
    (run.status === 'final_approved' || run.status === 'push_failed') &&
    !hasPr
  const isPushFailed = run.status === 'push_failed'
  const warning = hasGithubConfigWarning(project)

  return (
    <Card className="mb-4">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">GitHub PR</CardTitle>
            <CardDescription>
              Push the final-approved branch and create a pull request.
            </CardDescription>
          </div>
          <Badge variant={isPushFailed ? 'destructive' : 'secondary'}>
            {run.status}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <p className="font-medium">Branch</p>
            <p className="text-muted-foreground break-words">
              {run.branch_name || 'Not available yet'}
            </p>
          </div>
          <div>
            <p className="font-medium">PR Number</p>
            <p className="text-muted-foreground">
              {run.pr_number ? `#${run.pr_number}` : 'Not created yet'}
            </p>
          </div>
        </div>

        {run.pr_url && (
          <div>
            <p className="text-sm font-medium">Pull Request</p>
            <a
              href={run.pr_url}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-blue-600 underline break-words"
            >
              {run.pr_url}
            </a>
          </div>
        )}

        {warning && (
          <p className="text-sm font-medium text-yellow-600">
            GitHub configuration looks incomplete. Confirm the project has a
            token, owner, and repo before pushing.
          </p>
        )}

        {run.push_error && (
          <p className="text-sm font-medium text-red-500 whitespace-pre-wrap">
            {run.push_error}
          </p>
        )}
        {message && (
          <p className="text-sm font-medium text-green-600">{message}</p>
        )}
        {error && (
          <p className="text-sm font-medium text-red-500">{error}</p>
        )}

        {canPush && (
          <Button
            onClick={onPush}
            disabled={isPushing}
            className="w-fit bg-blue-600 hover:bg-blue-700 text-white"
          >
            {isPushing ? 'Pushing...' : 'Push and Create PR'}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
