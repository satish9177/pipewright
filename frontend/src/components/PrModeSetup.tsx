import type { PrMode, ProjectDetectResponse } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export interface GithubFields {
  github_owner: string
  github_repo: string
  github_base_branch: string
  github_token: string
}

interface PrModeOption {
  value: PrMode
  title: string
  description: string
}

const FORBIDDEN_BASE_BRANCHES = ['main', 'master', 'develop']
const SUGGESTED_BASE_BRANCH = 'pipewright-staging'

const PR_MODE_OPTIONS: PrModeOption[] = [
  {
    value: 'local_only',
    title: 'Local only',
    description:
      'Default. Pipewright commits to a local branch. No push, no token, no GitHub setup required.',
  },
  {
    value: 'github_cli',
    title: 'GitHub CLI',
    description:
      'Create the PR with the GitHub CLI (gh) after approval. No token is pasted into Pipewright.',
  },
  {
    value: 'manual_token',
    title: 'Manual token (advanced)',
    description:
      'Advanced fallback. Store a personal access token with owner/repo for PR creation.',
  },
]

interface PrModeSetupProps {
  detection: ProjectDetectResponse | null
  detecting: boolean
  detectError: string | null
  mode: PrMode
  onModeChange: (mode: PrMode) => void
  advancedOpen: boolean
  onAdvancedToggle: (open: boolean) => void
  fields: GithubFields
  onFieldChange: (field: keyof GithubFields, value: string) => void
  /** Settings only: report that a token is already stored, without ever showing it. */
  hasGithubToken?: boolean
  /** Settings only: token input keeps existing value when left blank. */
  tokenKeepsExisting?: boolean
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="font-medium">{label}</p>
      <p className="text-muted-foreground break-words">{value}</p>
    </div>
  )
}

function DetectionFacts({
  detection,
  detecting,
  detectError,
}: {
  detection: ProjectDetectResponse | null
  detecting: boolean
  detectError: string | null
}) {
  if (detecting) {
    return (
      <p className="text-sm text-muted-foreground">Detecting repository…</p>
    )
  }
  if (detectError) {
    return <p className="text-sm text-red-500">{detectError}</p>
  }
  if (!detection) {
    return (
      <p className="text-sm text-muted-foreground">
        Enter a repo path to detect Git and GitHub CLI settings.
      </p>
    )
  }

  const githubRepo =
    detection.is_github_remote && detection.github_owner && detection.github_repo
      ? `${detection.github_owner}/${detection.github_repo}`
      : 'No GitHub remote'

  return (
    <div className="grid gap-3 text-sm sm:grid-cols-2">
      <Fact
        label="Git repo"
        value={detection.is_git_repo ? 'Detected' : 'Not a Git repository'}
      />
      <Fact label="Git root" value={detection.git_root ?? 'Unknown'} />
      <Fact
        label="Current branch"
        value={detection.current_branch ?? 'Unknown / detached'}
      />
      <Fact label="GitHub remote" value={githubRepo} />
      <Fact
        label="GitHub CLI"
        value={
          detection.gh_installed
            ? detection.gh_authenticated
              ? 'Installed and authenticated'
              : 'Installed, not authenticated'
            : 'Not installed'
        }
      />
      <Fact
        label="Recommended mode"
        value={detection.recommended_pr_mode}
      />
    </div>
  )
}

export default function PrModeSetup({
  detection,
  detecting,
  detectError,
  mode,
  onModeChange,
  advancedOpen,
  onAdvancedToggle,
  fields,
  onFieldChange,
  hasGithubToken,
  tokenKeepsExisting,
}: PrModeSetupProps) {
  const recommendsGithubCli = detection?.recommended_pr_mode === 'github_cli'
  const ghUnavailable =
    detection != null && (!detection.gh_installed || !detection.gh_authenticated)
  const showManualFields = mode === 'manual_token' || advancedOpen
  const manualActive = mode === 'manual_token'
  const baseBranch = fields.github_base_branch.trim().toLowerCase()
  const baseBranchForbidden = FORBIDDEN_BASE_BRANCHES.includes(baseBranch)

  return (
    <div className="grid gap-5">
      <div className="grid gap-2">
        <p className="text-sm font-medium">Detected</p>
        <DetectionFacts
          detection={detection}
          detecting={detecting}
          detectError={detectError}
        />
      </div>

      <div className="grid gap-3">
        <p className="text-sm font-medium">PR creation mode</p>
        {PR_MODE_OPTIONS.map(option => {
          const isRecommended =
            option.value === 'github_cli' && recommendsGithubCli
          return (
            <label
              key={option.value}
              htmlFor={`pr-mode-${option.value}`}
              className="flex cursor-pointer items-start gap-3 rounded-md border p-3"
            >
              <input
                id={`pr-mode-${option.value}`}
                type="radio"
                name="pr_mode"
                className="mt-1"
                value={option.value}
                checked={mode === option.value}
                onChange={() => onModeChange(option.value)}
              />
              <span className="grid gap-1">
                <span className="flex items-center gap-2 text-sm font-medium">
                  {option.title}
                  {option.value === 'local_only' && (
                    <Badge variant="outline">Default</Badge>
                  )}
                  {isRecommended && (
                    <Badge
                      variant="outline"
                      className="border-green-200 bg-green-100 text-green-700"
                    >
                      Recommended
                    </Badge>
                  )}
                </span>
                <span className="text-xs text-muted-foreground">
                  {option.description}
                </span>
              </span>
            </label>
          )
        })}

        {mode === 'github_cli' && ghUnavailable && (
          <p className="text-sm font-medium text-yellow-600">
            Install GitHub CLI and run <code>gh auth login</code>, or continue
            in Local only mode.
          </p>
        )}
        {mode === 'github_cli' && (
          <p className="text-xs text-muted-foreground">
            No GitHub token is used in GitHub CLI mode — Pipewright
            authenticates through the <code>gh</code> CLI.
          </p>
        )}
      </div>

      <div className="grid gap-3">
        <button
          type="button"
          className="w-fit text-sm font-medium text-blue-600 underline"
          onClick={() => onAdvancedToggle(!advancedOpen)}
        >
          {showManualFields ? 'Hide advanced GitHub token settings' : 'Advanced: manual GitHub token'}
        </button>

        {showManualFields && (
          <div className="grid gap-4 rounded-md border p-4">
            {!manualActive && (
              <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
                These manual token settings are <strong>inactive</strong> because
                the selected PR mode is <strong>{mode}</strong>.{' '}
                {mode === 'github_cli'
                  ? 'GitHub CLI mode authenticates through gh and never uses a stored token.'
                  : 'Local only mode does not push or create pull requests.'}
              </p>
            )}
            {hasGithubToken !== undefined && (
              <Badge
                variant="outline"
                className={
                  hasGithubToken
                    ? 'w-fit border-green-200 bg-green-100 text-green-700'
                    : 'w-fit border-yellow-200 bg-yellow-100 text-yellow-800'
                }
              >
                {hasGithubToken ? 'TOKEN CONFIGURED' : 'NO TOKEN'}
              </Badge>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="github_owner">GitHub Owner</Label>
                <Input
                  id="github_owner"
                  placeholder="satish9177"
                  value={fields.github_owner}
                  onChange={e => onFieldChange('github_owner', e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="github_repo">Repository Name</Label>
                <Input
                  id="github_repo"
                  placeholder="ai-workflow-platform"
                  value={fields.github_repo}
                  onChange={e => onFieldChange('github_repo', e.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="github_base_branch">Base Branch for PRs</Label>
              <Input
                id="github_base_branch"
                placeholder={SUGGESTED_BASE_BRANCH}
                value={fields.github_base_branch}
                onChange={e =>
                  onFieldChange('github_base_branch', e.target.value)
                }
              />
              {baseBranchForbidden ? (
                <p className="text-xs font-medium text-red-500">
                  Pipewright never opens pull requests against{' '}
                  <code>main</code>, <code>master</code>, or{' '}
                  <code>develop</code>. Use a staging branch such as{' '}
                  <code>{SUGGESTED_BASE_BRANCH}</code>.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Suggested: <code>{SUGGESTED_BASE_BRANCH}</code>.
                </p>
              )}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="github_token">Personal Access Token</Label>
              <Input
                id="github_token"
                type="password"
                placeholder={
                  tokenKeepsExisting
                    ? 'Leave blank to keep existing token'
                    : 'ghp_...'
                }
                value={fields.github_token}
                onChange={e => onFieldChange('github_token', e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Only used for the manual token mode. The token is never returned
                by the API. GitHub CLI mode does not need a token.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
