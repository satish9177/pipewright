import { useState, type ChangeEvent, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { projectsApi, ProjectCreate } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

export default function NewProjectPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const [form, setForm] = useState<ProjectCreate>({
    name: '',
    repo_path: '',
    test_command: 'python --version',
    branch: 'main',
    description: '',
    github_token: '',
    github_owner: '',
    github_repo: '',
    github_base_branch: 'pipewright-staging',
  })

  const mutation = useMutation({
    mutationFn: projectsApi.create,
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      navigate(`/projects/${project.id}`)
    },
    onError: (err: any) => {
      setError(err.response?.data?.detail ?? 'Failed to create project')
    },
  })

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    const data = { ...form }
    if (!data.github_token) delete data.github_token
    if (!data.github_owner) delete data.github_owner
    if (!data.github_repo) delete data.github_repo
    mutation.mutate(data)
  }

  const update = (field: keyof ProjectCreate) =>
    (e: ChangeEvent<HTMLInputElement>) =>
      setForm(prev => ({ ...prev, [field]: e.target.value }))

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h2 className="text-2xl font-bold">New Project</h2>
        <p className="text-muted-foreground text-sm mt-1">
          Register a repository to run the pipeline against
        </p>
      </div>

      <form onSubmit={handleSubmit}>
        <Card className="mb-4">
          <CardHeader>
            <CardTitle className="text-base">Repository</CardTitle>
            <CardDescription>
              Basic project configuration
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="name">Project Name</Label>
              <Input
                id="name"
                placeholder="AI Workflow Platform"
                value={form.name}
                onChange={update('name')}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="repo_path">Local Repo Path</Label>
              <Input
                id="repo_path"
                placeholder="C:\\Users\\Hp\\ai-workflow-platform"
                value={form.repo_path}
                onChange={update('repo_path')}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="test_command">Test Command</Label>
              <Input
                id="test_command"
                placeholder="python --version"
                value={form.test_command}
                onChange={update('test_command')}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="branch">Default Branch</Label>
              <Input
                id="branch"
                placeholder="main"
                value={form.branch}
                onChange={update('branch')}
              />
            </div>
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="text-base">
              GitHub (Optional)
            </CardTitle>
            <CardDescription>
              Required for automatic PR creation after approval
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="github_token">
                Personal Access Token
              </Label>
              <Input
                id="github_token"
                type="password"
                placeholder="ghp_..."
                value={form.github_token}
                onChange={update('github_token')}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="github_owner">GitHub Owner</Label>
                <Input
                  id="github_owner"
                  placeholder="satish9177"
                  value={form.github_owner}
                  onChange={update('github_owner')}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="github_repo">Repository Name</Label>
                <Input
                  id="github_repo"
                  placeholder="ai-workflow-platform"
                  value={form.github_repo}
                  onChange={update('github_repo')}
                />
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="github_base_branch">
                Base Branch for PRs
              </Label>
              <Input
                id="github_base_branch"
                placeholder="pipewright-staging"
                value={form.github_base_branch}
                onChange={update('github_base_branch')}
              />
            </div>
          </CardContent>
        </Card>

        {error && (
          <p className="text-red-500 text-sm mb-4">{error}</p>
        )}

        <div className="flex gap-3">
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? 'Creating...' : 'Create Project'}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => navigate('/projects')}
          >
            Cancel
          </Button>
        </div>
      </form>
    </div>
  )
}
