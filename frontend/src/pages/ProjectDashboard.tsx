import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  projectsApi,
  runsApi,
  isNeedsClarification,
  PipelineRun,
  NeedsClarificationResponse,
} from '@/api/client'
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
import ProjectSettingsPanel from '@/components/ProjectSettingsPanel'

export default function ProjectDashboard() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feature, setFeature] = useState('')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [lastRunId, setLastRunId] = useState<string | null>(null)
  const [clarification, setClarification] =
    useState<NeedsClarificationResponse | null>(null)

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
      setSubmitError(null)
      if (isNeedsClarification(data)) {
        // Vague implementation request: no run was created. Ask for details
        // instead of navigating to a run that does not exist.
        setClarification(data)
        setLastRunId(null)
        return
      }
      setClarification(null)
      setLastRunId(data.run_id)
      setFeature('')
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${data.run_id}`)
    },
    onError: (error: unknown) => {
      setClarification(null)
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
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">{project.name}</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {project.repo_path}
          </p>
          {!project.has_github_token && (
            <p className="mt-2 text-sm text-yellow-700">
              GitHub token is not configured. Push/create PR will need project
              GitHub settings before it can succeed.
            </p>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate('/projects')}
        >
          All Projects
        </Button>
      </div>

      <ProjectSettingsPanel project={project} />

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Project Memory</CardTitle>
          <CardDescription>
            Manage advisory project memory and bootstrap suggestions in the
            dedicated Memory tab.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            onClick={() => navigate(`/memory?projectId=${project.id}`)}
          >
            Open Memory
          </Button>
        </CardContent>
      </Card>

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
              rows={5}
              className="resize-none"
            />
            {submitError && (
              <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                {submitError}
              </div>
            )}
            {clarification && (
              <div className="rounded border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-900">
                <p className="font-semibold">Needs clarification</p>
                <p className="mt-1">{clarification.message}</p>
                {clarification.missing_details.length > 0 && (
                  <>
                    <p className="mt-2 font-medium">Please specify:</p>
                    <ul className="mt-1 list-disc pl-5">
                      {clarification.missing_details.map((detail) => (
                        <li key={detail}>{detail}</li>
                      ))}
                    </ul>
                  </>
                )}
                {clarification.examples.length > 0 && (
                  <>
                    <p className="mt-2 font-medium">Example valid requests:</p>
                    <ul className="mt-1 list-disc pl-5">
                      {clarification.examples.map((example) => (
                        <li key={example}>{example}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
            {lastRunId && (
              <p className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700">
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
              className="w-fit"
            >
              {runMutation.isPending ? 'Creating...' : 'Create Chunked Run'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div>
        <div className="mb-3">
          <h3 className="font-semibold">Recent Runs</h3>
          <p className="text-xs text-muted-foreground">
            Existing legacy and chunked runs remain available here.
          </p>
        </div>
        {projectRuns.length === 0 && (
          <Card className="border-dashed">
            <CardContent className="py-6">
              <p className="text-sm font-medium">No runs yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Describe a feature above to create the first chunked run.
              </p>
            </CardContent>
          </Card>
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
                      {run.current_step || 'not started'}
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
