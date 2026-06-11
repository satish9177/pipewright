import { useState, type ChangeEvent, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  projectsApi,
  type PrMode,
  type ProjectCreateRequest,
  type ProjectDetectResponse,
} from '@/api/client'
import PrModeSetup, { type GithubFields } from '@/components/PrModeSetup'
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

interface BasicFields {
  name: string
  repo_path: string
  test_command: string
  branch: string
  description: string
}

// The placeholder default for a brand-new form. Detection may prefill over this
// (it is not a real test command), but never over a value the user has typed.
const DEFAULT_TEST_COMMAND = 'python --version'

export default function NewProjectPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const [basic, setBasic] = useState<BasicFields>({
    name: '',
    repo_path: '',
    test_command: DEFAULT_TEST_COMMAND,
    branch: 'main',
    description: '',
  })
  const [github, setGithub] = useState<GithubFields>({
    github_owner: '',
    github_repo: '',
    github_base_branch: 'pipewright-staging',
    github_token: '',
  })
  const [mode, setMode] = useState<PrMode>('local_only')
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const [detection, setDetection] = useState<ProjectDetectResponse | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [detectError, setDetectError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: projectsApi.create,
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      navigate(`/projects/${project.id}`)
    },
    onError: (err: unknown) => {
      const detail =
        typeof err === 'object' && err !== null && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data
              ?.detail
          : undefined
      setError(detail ?? 'Failed to create project')
    },
  })

  const updateBasic = (field: keyof BasicFields) =>
    (e: ChangeEvent<HTMLInputElement>) =>
      setBasic(prev => ({ ...prev, [field]: e.target.value }))

  const updateGithub = (field: keyof GithubFields, value: string) =>
    setGithub(prev => ({ ...prev, [field]: value }))

  const runDetection = async () => {
    const repoPath = basic.repo_path.trim()
    if (!repoPath) return
    setDetecting(true)
    setDetectError(null)
    try {
      const result = await projectsApi.detect(repoPath)
      setDetection(result)
      // Auto-fill owner/repo from detection without overwriting user edits.
      setGithub(prev => ({
        ...prev,
        github_owner: prev.github_owner || result.github_owner || '',
        github_repo: prev.github_repo || result.github_repo || '',
      }))
      // Prefill the detected test command, but only when the user has not yet
      // typed one (still empty or the untouched placeholder default). Never
      // overwrite a command the user has entered.
      if (result.suggested_test_command) {
        setBasic(prev =>
          prev.test_command.trim() === '' ||
          prev.test_command === DEFAULT_TEST_COMMAND
            ? { ...prev, test_command: result.suggested_test_command as string }
            : prev,
        )
      }
    } catch {
      setDetection(null)
      setDetectError('Detection failed. You can still configure the project manually.')
    } finally {
      setDetecting(false)
    }
  }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    setError(null)

    const data: ProjectCreateRequest = {
      name: basic.name,
      repo_path: basic.repo_path,
      test_command: basic.test_command,
      branch: basic.branch,
      description: basic.description,
      pr_mode: mode,
    }

    // Only send GitHub fields for the modes that use them.
    if (mode !== 'local_only') {
      if (github.github_owner) data.github_owner = github.github_owner
      if (github.github_repo) data.github_repo = github.github_repo
      if (github.github_base_branch)
        data.github_base_branch = github.github_base_branch
    }
    if (mode === 'manual_token' && github.github_token) {
      data.github_token = github.github_token
    }

    mutation.mutate(data)
  }

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
            <CardDescription>Basic project configuration</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="name">Project Name</Label>
              <Input
                id="name"
                placeholder="AI Workflow Platform"
                value={basic.name}
                onChange={updateBasic('name')}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="repo_path">Local repository path</Label>
              <div className="flex items-center gap-2">
                <Input
                  id="repo_path"
                  placeholder="C:\\Users\\satis\\Projects\\pipewright"
                  value={basic.repo_path}
                  onChange={updateBasic('repo_path')}
                  onBlur={runDetection}
                  required
                  className="flex-1"
                />
                <Button
                  type="button"
                  variant="outline"
                  onClick={runDetection}
                  disabled={detecting || !basic.repo_path.trim()}
                >
                  {detecting ? 'Detecting…' : 'Detect from folder'}
                </Button>
              </div>
              <div className="grid gap-1 text-xs text-muted-foreground">
                <p>
                  Open your project folder in a terminal and run{' '}
                  <code>pwd</code>, then paste the output here. This should be
                  the folder that contains the Git repo.
                </p>
                <p>Examples:</p>
                <ul className="list-disc pl-5">
                  <li>
                    Windows PowerShell:{' '}
                    <code>C:\Users\satis\Projects\pipewright</code>
                  </li>
                  <li>
                    Mac/Linux: <code>/Users/satish/projects/pipewright</code>
                  </li>
                </ul>
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="branch">Default Branch</Label>
              <Input
                id="branch"
                placeholder="main"
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
                <Label htmlFor="test_command">Test Command</Label>
                <Input
                  id="test_command"
                  placeholder="python --version"
                  value={basic.test_command}
                  onChange={updateBasic('test_command')}
                  required
                />
                {detection?.suggested_test_command && (
                  <p className="text-xs text-muted-foreground">
                    Suggested from your repo:{' '}
                    <code>{detection.suggested_test_command}</code>. You can edit
                    or replace it.
                  </p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="text-base">PR Creation</CardTitle>
            <CardDescription>
              Choose how Pipewright handles pull requests after final approval.
            </CardDescription>
          </CardHeader>
          <CardContent>
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
            />
          </CardContent>
        </Card>

        {error && <p className="text-red-500 text-sm mb-4">{error}</p>}

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
