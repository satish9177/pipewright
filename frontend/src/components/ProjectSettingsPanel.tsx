import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type {
  PrMode,
  Project,
  ProjectDetectResponse,
  ProjectReindexResponse,
  ProjectUpdateRequest,
} from '@/api/client'
import { projectsApi } from '@/api/client'
import PrModeSetup, { type GithubFields } from '@/components/PrModeSetup'
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
import { Textarea } from '@/components/ui/textarea'

interface ProjectSettingsPanelProps {
  project: Project
}

interface ProjectSettingsForm {
  name: string
  description: string
  test_command: string
  branch: string
}

function basicFromProject(project: Project): ProjectSettingsForm {
  return {
    name: project.name ?? '',
    description: project.description ?? '',
    test_command: project.test_command ?? '',
    branch: project.branch ?? '',
  }
}

function githubFromProject(project: Project): GithubFields {
  return {
    github_owner: project.github_owner ?? '',
    github_repo: project.github_repo ?? '',
    github_base_branch: project.github_base_branch ?? 'pipewright-staging',
    github_token: '',
  }
}

function getErrorMessage(error: unknown) {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } })
      .response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return 'Failed to update project settings'
}

function normalizeMode(value: string | undefined): PrMode {
  if (value === 'github_cli' || value === 'manual_token') return value
  return 'local_only'
}

function getReindexErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (
      error as { response?: { status?: number; data?: { detail?: unknown } } }
    ).response
    if (response?.status === 409) {
      return 'A run is active for this project — re-index when it finishes.'
    }
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return 'Re-index failed.'
}

function indexStatusLabel(status: string | undefined): string {
  if (status === 'indexed') return 'Indexed'
  if (status === 'not_indexed') return 'Not indexed'
  return status || 'Unknown'
}

// The backend returns a SQLite timestamp like "2026-06-02 12:34:56" (no T/Z).
// Normalize the space to a "T" so Safari/strict parsers accept it, and fall
// back to the raw string if it still cannot be parsed. Never throws.
function formatIndexedAt(value: string | null | undefined): string {
  if (!value) return 'Never'
  const candidate = value.includes('T') ? value : value.replace(' ', 'T')
  const date = new Date(candidate)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export default function ProjectSettingsPanel({
  project,
}: ProjectSettingsPanelProps) {
  const queryClient = useQueryClient()
  const [basic, setBasic] = useState<ProjectSettingsForm>(
    basicFromProject(project),
  )
  const [github, setGithub] = useState<GithubFields>(githubFromProject(project))
  const [mode, setMode] = useState<PrMode>(normalizeMode(project.pr_mode))
  const [advancedOpen, setAdvancedOpen] = useState(
    normalizeMode(project.pr_mode) === 'manual_token',
  )
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [detection, setDetection] = useState<ProjectDetectResponse | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [detectError, setDetectError] = useState<string | null>(null)

  const [reindexMessage, setReindexMessage] = useState<string | null>(null)
  const [reindexError, setReindexError] = useState<string | null>(null)

  const indexStatusQuery = useQuery({
    queryKey: ['project-index', project.id],
    queryFn: () => projectsApi.getIndexStatus(project.id),
  })

  const reindexMutation = useMutation({
    mutationFn: () => projectsApi.reindex(project.id),
    onSuccess: (data: ProjectReindexResponse) => {
      setReindexError(null)
      setReindexMessage(
        data.message || `Re-indexed ${data.files_indexed} files.`,
      )
      // Reflect the fresh count/time immediately, then keep the cache honest.
      queryClient.setQueryData(['project-index', project.id], {
        project_id: data.project_id,
        files_indexed: data.files_indexed,
        indexed_at: data.indexed_at,
        status: data.files_indexed > 0 ? 'indexed' : 'not_indexed',
      })
      queryClient.invalidateQueries({ queryKey: ['project-index', project.id] })
    },
    onError: (reindexErr: unknown) => {
      setReindexMessage(null)
      setReindexError(getReindexErrorMessage(reindexErr))
    },
  })

  const handleReindex = () => {
    setReindexMessage(null)
    setReindexError(null)
    reindexMutation.mutate()
  }

  useEffect(() => {
    setBasic(basicFromProject(project))
    setGithub(githubFromProject(project))
    setMode(normalizeMode(project.pr_mode))
    setAdvancedOpen(normalizeMode(project.pr_mode) === 'manual_token')
  }, [project])

  const mutation = useMutation({
    mutationFn: (data: ProjectUpdateRequest) =>
      projectsApi.update(project.id, data),
    onSuccess: () => {
      setMessage('Project settings saved.')
      setError(null)
      setGithub(previous => ({ ...previous, github_token: '' }))
      queryClient.invalidateQueries({ queryKey: ['project', project.id] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (updateError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(updateError))
    },
  })

  const updateBasic = (field: keyof ProjectSettingsForm) =>
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setBasic(previous => ({ ...previous, [field]: event.target.value }))
    }

  const updateGithub = (field: keyof GithubFields, value: string) =>
    setGithub(previous => ({ ...previous, [field]: value }))

  const runDetection = async () => {
    setDetecting(true)
    setDetectError(null)
    try {
      const result = await projectsApi.detect(project.repo_path)
      setDetection(result)
    } catch {
      setDetection(null)
      setDetectError('Detection failed. Settings can still be edited manually.')
    } finally {
      setDetecting(false)
    }
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setMessage(null)
    setError(null)

    const data: ProjectUpdateRequest = {
      name: basic.name,
      description: basic.description,
      test_command: basic.test_command,
      branch: basic.branch,
      pr_mode: mode,
      github_owner: github.github_owner,
      github_repo: github.github_repo,
      github_base_branch: github.github_base_branch,
    }

    if (github.github_token.trim()) {
      data.github_token = github.github_token
    }

    mutation.mutate(data)
  }

  return (
    <Card className="mb-6">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Project Settings</CardTitle>
            <CardDescription>
              Configure the local repo, test command, and PR creation mode.
            </CardDescription>
          </div>
          <Badge variant="outline">{`Saved PR mode: ${project.pr_mode}`}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="mb-4 grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <p className="font-medium">Repo Path</p>
            <p className="text-muted-foreground break-words">
              {project.repo_path}
            </p>
          </div>
          <div>
            <p className="font-medium">Branch</p>
            <p className="text-muted-foreground break-words">
              {project.branch || 'Not set'}
            </p>
          </div>
        </div>

        <div className="mb-6 grid gap-3 rounded-md border p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-sm font-medium">Repository index</p>
              <p className="text-xs text-muted-foreground">
                The index is a cached map of your repo's files. Re-index after
                adding, renaming, or deleting files outside Pipewright.
              </p>
            </div>
            {indexStatusQuery.data && (
              <Badge variant="outline">
                {indexStatusLabel(indexStatusQuery.data.status)}
              </Badge>
            )}
          </div>

          {indexStatusQuery.isLoading ? (
            <p className="text-sm text-muted-foreground">
              Loading index status…
            </p>
          ) : indexStatusQuery.isError ? (
            <p className="text-sm text-muted-foreground">
              Index status unavailable.
            </p>
          ) : (
            <div className="grid gap-1 text-sm sm:grid-cols-2">
              <div>
                <p className="font-medium">Files indexed</p>
                <p className="text-muted-foreground">
                  {indexStatusQuery.data?.files_indexed ?? 0}
                </p>
              </div>
              <div>
                <p className="font-medium">Last indexed</p>
                <p className="text-muted-foreground">
                  {formatIndexedAt(indexStatusQuery.data?.indexed_at)}
                </p>
              </div>
            </div>
          )}

          {reindexMessage && (
            <p className="text-sm font-medium text-green-600">
              {reindexMessage}
            </p>
          )}
          {reindexError && (
            <p className="text-sm font-medium text-red-500">{reindexError}</p>
          )}

          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-fit"
            onClick={handleReindex}
            disabled={reindexMutation.isPending}
          >
            {reindexMutation.isPending
              ? 'Re-indexing…'
              : 'Re-index repository'}
          </Button>
        </div>

        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="project-name">Name</Label>
            <Input
              id="project-name"
              value={basic.name}
              onChange={updateBasic('name')}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="project-description">Description</Label>
            <Textarea
              id="project-description"
              value={basic.description}
              onChange={updateBasic('description')}
              rows={3}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="project-branch">Branch</Label>
            <Input
              id="project-branch"
              value={basic.branch}
              onChange={updateBasic('branch')}
            />
          </div>

          <div className="grid gap-3 rounded-md border p-4">
            <div>
              <p className="text-sm font-medium">Project checks</p>
              <p className="text-xs text-muted-foreground">
                Pipewright runs this command after applying code changes.
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="project-test-command">Test Command</Label>
              <Input
                id="project-test-command"
                value={basic.test_command}
                onChange={updateBasic('test_command')}
              />
            </div>
          </div>

          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium">PR creation</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={runDetection}
              disabled={detecting}
            >
              {detecting ? 'Detecting…' : 'Detect from folder'}
            </Button>
          </div>

          <PrModeSetup
            detection={detection}
            detecting={detecting}
            detectError={detectError}
            mode={mode}
            onModeChange={setMode}
            advancedOpen={advancedOpen}
            onAdvancedToggle={setAdvancedOpen}
            fields={github}
            onFieldChange={updateGithub}
            hasGithubToken={project.has_github_token}
            tokenKeepsExisting
          />

          {message && (
            <p className="text-sm font-medium text-green-600">{message}</p>
          )}
          {error && (
            <p className="text-sm font-medium text-red-500">{error}</p>
          )}

          <Button
            type="submit"
            disabled={mutation.isPending}
            className="w-fit"
          >
            {mutation.isPending ? 'Saving...' : 'Save Project Settings'}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
