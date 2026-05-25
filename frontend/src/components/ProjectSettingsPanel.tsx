import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { Project, ProjectUpdateRequest } from '@/api/client'
import { projectsApi } from '@/api/client'
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
  github_owner: string
  github_repo: string
  github_base_branch: string
  github_token: string
}

function formFromProject(project: Project): ProjectSettingsForm {
  return {
    name: project.name ?? '',
    description: project.description ?? '',
    test_command: project.test_command ?? '',
    branch: project.branch ?? '',
    github_owner: project.github_owner ?? '',
    github_repo: project.github_repo ?? '',
    github_base_branch: project.github_base_branch ?? '',
    github_token: '',
  }
}

function getErrorMessage(error: unknown) {
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

  return 'Failed to update project settings'
}

export default function ProjectSettingsPanel({
  project,
}: ProjectSettingsPanelProps) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<ProjectSettingsForm>(
    formFromProject(project)
  )
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setForm(formFromProject(project))
  }, [project])

  const mutation = useMutation({
    mutationFn: (data: ProjectUpdateRequest) =>
      projectsApi.update(project.id, data),
    onSuccess: () => {
      setMessage('Project settings saved.')
      setError(null)
      setForm(previous => ({ ...previous, github_token: '' }))
      queryClient.invalidateQueries({ queryKey: ['project', project.id] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (updateError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(updateError))
    },
  })

  const update = (field: keyof ProjectSettingsForm) =>
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setForm(previous => ({ ...previous, [field]: event.target.value }))
    }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setMessage(null)
    setError(null)

    const data: ProjectUpdateRequest = {
      name: form.name,
      description: form.description,
      test_command: form.test_command,
      branch: form.branch,
      github_owner: form.github_owner,
      github_repo: form.github_repo,
      github_base_branch: form.github_base_branch,
    }

    if (form.github_token.trim()) {
      data.github_token = form.github_token
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
              Configure the local repo, test command, and GitHub PR settings.
            </CardDescription>
          </div>
          <Badge
            variant="outline"
            className={
              project.has_github_token
                ? 'border-green-200 bg-green-100 text-green-700'
                : 'border-yellow-200 bg-yellow-100 text-yellow-800'
            }
          >
            {project.has_github_token ? 'TOKEN CONFIGURED' : 'NO TOKEN'}
          </Badge>
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
            <p className="font-medium">Test Command</p>
            <p className="text-muted-foreground break-words">
              {project.test_command}
            </p>
          </div>
          <div>
            <p className="font-medium">Branch</p>
            <p className="text-muted-foreground break-words">
              {project.branch || 'Not set'}
            </p>
          </div>
          <div>
            <p className="font-medium">GitHub Repository</p>
            <p className="text-muted-foreground break-words">
              {project.github_owner && project.github_repo
                ? `${project.github_owner}/${project.github_repo}`
                : 'Not configured'}
            </p>
          </div>
          <div>
            <p className="font-medium">GitHub Base Branch</p>
            <p className="text-muted-foreground break-words">
              {project.github_base_branch || 'Not set'}
            </p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="project-name">Name</Label>
            <Input
              id="project-name"
              value={form.name}
              onChange={update('name')}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="project-description">Description</Label>
            <Textarea
              id="project-description"
              value={form.description}
              onChange={update('description')}
              rows={3}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="project-test-command">Test Command</Label>
              <Input
                id="project-test-command"
                value={form.test_command}
                onChange={update('test_command')}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="project-branch">Branch</Label>
              <Input
                id="project-branch"
                value={form.branch}
                onChange={update('branch')}
              />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="project-github-owner">GitHub Owner</Label>
              <Input
                id="project-github-owner"
                value={form.github_owner}
                onChange={update('github_owner')}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="project-github-repo">GitHub Repo</Label>
              <Input
                id="project-github-repo"
                value={form.github_repo}
                onChange={update('github_repo')}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="project-github-base-branch">
              GitHub Base Branch
            </Label>
            <Input
              id="project-github-base-branch"
              value={form.github_base_branch}
              onChange={update('github_base_branch')}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="project-github-token">GitHub Token</Label>
            <Input
              id="project-github-token"
              type="password"
              value={form.github_token}
              onChange={update('github_token')}
              placeholder="Leave blank to keep existing token"
            />
            <p className="text-xs text-muted-foreground">
              GitHub token is used for push/PR creation and is never returned
              by the API.
            </p>
          </div>

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
