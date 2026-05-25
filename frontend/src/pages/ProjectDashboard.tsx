import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { projectsApi, runsApi, PipelineRun } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import RunStatusBadge from '@/components/RunStatusBadge'

export default function ProjectDashboard() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feature, setFeature] = useState('')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [lastRunId, setLastRunId] = useState<string | null>(null)

  const { data: project, isLoading: projectLoading } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => projectsApi.get(projectId!),
    enabled: !!projectId,
  })

  const { data: runs } = useQuery({
    queryKey: ['runs'],
    queryFn: runsApi.list,
    refetchInterval: 3000,
  })

  const projectRuns = runs?.filter(
    (r: PipelineRun) => r.project_id === projectId
  ) ?? []

  function getSubmitError(error: unknown) {
    if (
      typeof error === 'object' &&
      error !== null &&
      'response' in error
    ) {
      const response = (error as { response?: { data?: { detail?: unknown } } })
        .response
      if (typeof response?.data?.detail === 'string') {
        return response.data.detail
      }
    }

    return 'Failed to create chunked run'
  }

  const runMutation = useMutation({
    mutationFn: () => runsApi.createChunkedRun(projectId!, feature),
    onSuccess: (data) => {
      setLastRunId(data.run_id)
      setFeature('')
      setSubmitError(null)
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${data.run_id}`)
    },
    onError: (error: unknown) => {
      setSubmitError(getSubmitError(error))
    },
  })

  if (projectLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    )
  }

  if (!project) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-red-500">Project not found</p>
      </div>
    )
  }

  return (
    <div className="max-w-3xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">{project.name}</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {project.repo_path}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate('/projects')}
        >
          All Projects
        </Button>
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Create Chunked Run</CardTitle>
          <CardDescription>
            Pipewright will generate a chunk plan for approval before executing
            changes.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3">
            <Label htmlFor="feature">Feature Description</Label>
            <Textarea
              id="feature"
              placeholder="Add a GET /ping endpoint that returns status ok and current timestamp..."
              value={feature}
              onChange={e => setFeature(e.target.value)}
              rows={4}
              className="resize-none"
            />
            {submitError && (
              <p className="text-red-500 text-sm">{submitError}</p>
            )}
            {lastRunId && (
              <p className="text-sm text-muted-foreground">
                Chunked run created.{' '}
                <button
                  className="underline text-primary"
                  onClick={() => navigate(`/runs/${lastRunId}`)}
                >
                  View run
                </button>
              </p>
            )}
            <Button
              onClick={() => runMutation.mutate()}
              disabled={!feature.trim() || runMutation.isPending}
            >
              {runMutation.isPending ? 'Creating...' : 'Create Chunked Run'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div>
        <h3 className="font-semibold mb-3">Recent Runs</h3>
        {projectRuns.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No runs yet. Submit a feature above.
          </p>
        )}
        <div className="grid gap-2">
          {projectRuns.map((run: PipelineRun) => (
            <Card
              key={run.id}
              className="cursor-pointer hover:border-primary transition-colors"
              onClick={() => navigate(`/runs/${run.id}`)}
            >
              <CardContent className="py-3 px-4">
                <div className="flex items-center justify-between gap-4">
                  <p className="text-sm truncate flex-1">
                    {run.feature_description}
                  </p>
                  <div className="flex items-center gap-3 shrink-0">
                    <span className="text-xs text-muted-foreground">
                      {run.current_step}
                    </span>
                    <RunStatusBadge status={run.status} />
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  )
}
