import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  projectsApi,
  runsApi,
  isNeedsClarification,
  isStaleIndexResponse,
  isUnsafeStartBranchResponse,
  isModeConflictResponse,
  PipelineRun,
  NeedsClarificationResponse,
  ChunkedRunResult,
  IntentSuggestionResponse,
  ModeConflictResponse,
  ModeConflictOption,
  RunRequestedMode,
  StaleIndexResponse,
  UnsafeStartBranchResponse,
} from '@/api/client'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
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

// #42D: the three concrete start modes shown at run creation. "auto" exists in
// the API for old/legacy clients but is intentionally not offered here — the
// visible selected mode is the source of truth.
const MODE_OPTIONS: {
  value: Exclude<RunRequestedMode, 'auto'>
  title: string
  copy: string
}[] = [
  {
    value: 'report_only',
    title: 'Read-only report',
    copy: 'Analyze and explain only. No code changes, tests, commits, or PR.',
  },
  {
    value: 'plan_only',
    title: 'Plan only',
    copy: 'Create an implementation plan. No code changes.',
  },
  {
    value: 'implementation',
    title: 'Implement with approval',
    copy: 'Create a chunk plan first. Code runs only after you approve the plan.',
  },
]

// #43A: avoid noisy suggestion calls. Don't query for very short text, and wait
// for a typing pause before asking. The endpoint is read-only/advisory, so a
// missed or stale suggestion only means "no badge" — never a blocked run.
const SUGGESTION_MIN_CHARS = 12
const SUGGESTION_DEBOUNCE_MS = 400

export default function ProjectDashboard() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feature, setFeature] = useState('')
  // Default to "implementation"; it still requires chunk-plan approval before
  // any code runs (no auto-execution). A classifier-suggested default is #42+.
  const [requestedMode, setRequestedMode] =
    useState<RunRequestedMode>('implementation')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [lastRunId, setLastRunId] = useState<string | null>(null)
  const [clarification, setClarification] =
    useState<NeedsClarificationResponse | null>(null)
  const [noRunResponse, setNoRunResponse] =
    useState<StaleIndexResponse | UnsafeStartBranchResponse | null>(null)
  const [modeConflict, setModeConflict] =
    useState<ModeConflictResponse | null>(null)
  const [selectionReply, setSelectionReply] = useState('')
  // #43A: advisory mode suggestion. The visible selected mode stays the source
  // of truth; the suggestion only pre-highlights a mode and shows a badge.
  const [suggestion, setSuggestion] =
    useState<IntentSuggestionResponse | null>(null)
  // Once the user manually changes the mode for the current request, suggestion
  // arrivals must not move the selection again (no silent override). Reset only
  // when the request text is cleared (a fresh request). A ref so the debounced
  // callback reads the live value without re-subscribing the effect.
  const userChangedModeRef = useRef(false)
  // Monotonic request id: a slow/older suggestion response must never overwrite
  // the result for newer text the user has since typed.
  const suggestionSeqRef = useRef(0)

  // Debounced, deterministic suggestion fetch. Failures are swallowed (advisory
  // only); a stale response is dropped via the sequence guard.
  useEffect(() => {
    const text = feature.trim()
    if (text.length === 0) {
      // Cleared input is a brand-new request: allow auto-select again.
      userChangedModeRef.current = false
    }
    // All state changes happen inside the debounced callback (never
    // synchronously in the effect body) to avoid cascading renders.
    const seq = ++suggestionSeqRef.current
    const handle = setTimeout(() => {
      if (seq !== suggestionSeqRef.current) return
      if (text.length < SUGGESTION_MIN_CHARS) {
        setSuggestion(null)
        return
      }
      runsApi
        .intentSuggestion(text)
        .then(result => {
          if (seq !== suggestionSeqRef.current) return
          setSuggestion(result)
          // Auto-select the suggested concrete mode only if the user has not
          // taken manual control of the selection for this request.
          if (!userChangedModeRef.current && result.suggested_mode) {
            setRequestedMode(result.suggested_mode)
          }
        })
        .catch(() => {
          // Never block run creation; just show no badge on failure.
          if (seq === suggestionSeqRef.current) setSuggestion(null)
        })
    }, SUGGESTION_DEBOUNCE_MS)
    return () => clearTimeout(handle)
  }, [feature])

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

  function noRunTitle(
    response: StaleIndexResponse | UnsafeStartBranchResponse,
  ) {
    if (isStaleIndexResponse(response)) return 'Repository index is stale'
    return 'Unsafe start branch'
  }

  function noRunGuidance(
    response: StaleIndexResponse | UnsafeStartBranchResponse,
  ) {
    if (isStaleIndexResponse(response)) {
      return 'Re-index the repository, then submit again.'
    }
    if (response.current_branch?.startsWith('pipewright/')) {
      return 'Checkout the branch you want Pipewright to start from, then submit again.'
    }
    if (response.current_branch == null) {
      return 'Detached HEAD is not a safe start point. Checkout the branch you want Pipewright to start from, then submit again.'
    }
    return 'Checkout the branch you want Pipewright to start from, then submit again.'
  }

  // Shared outcome handler for both the initial request and a clarification
  // selection: a real plan navigates to the run; a needs_clarification response
  // re-renders the clarification (with a refreshed clarification_id).
  function handleChunkedResult(data: ChunkedRunResult) {
    setSubmitError(null)
    setNoRunResponse(null)
    setModeConflict(null)
    if (isModeConflictResponse(data)) {
      // No run was created. Show the conflict warning + the backend's options;
      // the user explicitly confirms or switches (never auto-confirmed here).
      setClarification(null)
      setSelectionReply('')
      setLastRunId(null)
      setModeConflict(data)
      return
    }
    if (isNeedsClarification(data)) {
      // No run was created. Ask for details / a file choice instead of
      // navigating to a run that does not exist.
      setClarification(data)
      setSelectionReply('')
      setLastRunId(null)
      return
    }
    if (isStaleIndexResponse(data) || isUnsafeStartBranchResponse(data)) {
      setClarification(null)
      setSelectionReply('')
      setLastRunId(null)
      setNoRunResponse(data)
      return
    }
    if (!data.run_id) {
      setClarification(null)
      setSelectionReply('')
      setLastRunId(null)
      setSubmitError('Run was not created. Review the repository state and try again.')
      return
    }
    setClarification(null)
    setSelectionReply('')
    setLastRunId(data.run_id)
    setFeature('')
    queryClient.invalidateQueries({ queryKey: ['runs'] })
    navigate(`/runs/${data.run_id}`)
  }

  const runMutation = useMutation({
    // #42E: a conflict option re-submits with an explicit mode + confirm flag.
    // The plain Start button omits variables and uses the visible selected mode
    // with confirm_conflict=false. Variables are passed explicitly (not read
    // from state) so a just-switched mode is never stale.
    mutationFn: (vars?: {
      mode?: RunRequestedMode
      confirmConflict?: boolean
    }) =>
      runsApi.createChunkedRun(
        projectId!,
        feature,
        vars?.mode ?? requestedMode,
        vars?.confirmConflict ?? false,
      ),
    onSuccess: handleChunkedResult,
    onError: (error: unknown) => {
      setClarification(null)
      setNoRunResponse(null)
      setModeConflict(null)
      setSubmitError(getSubmitError(error))
    },
  })

  // #42E: act on a backend-offered conflict option. The selected mode is moved
  // to the chosen option first (visible state — no silent change), the warning
  // is cleared, then the run is re-submitted with that option's confirm flag.
  // "implementation" carries confirm_conflict=true (continue); a safer option
  // such as "report_only" carries confirm_conflict=false (switch + honor).
  function chooseConflictOption(option: ModeConflictOption) {
    const mode = option.mode as RunRequestedMode
    setRequestedMode(mode)
    setModeConflict(null)
    runMutation.mutate({ mode, confirmConflict: option.confirm_conflict })
  }

  // Forward the user's raw reply (a candidate path, or typed "1"/"yes 1"/
  // "use README.md") to the backend selection endpoint. The frontend never
  // parses the reply itself; the backend maps it within the clarification's
  // candidate set.
  const selectMutation = useMutation({
    mutationFn: (selection: string) => {
      const clarificationId = clarification?.clarification_id
      if (!clarificationId) {
        return Promise.reject(new Error('Missing clarification context'))
      }
      return runsApi.selectClarification(clarificationId, projectId!, selection)
    },
    onSuccess: handleChunkedResult,
    onError: (error: unknown) => {
      setNoRunResponse(null)
      setSubmitError(getSubmitError(error))
    },
  })

  const reindexMutation = useMutation({
    mutationFn: () => projectsApi.reindex(projectId!),
    onSuccess: () => {
      setSubmitError(null)
      setClarification(null)
      setSelectionReply('')
      setModeConflict(null)
      setNoRunResponse({
        status: 'stale_index',
        outcome: 'stale_index',
        run_created: false,
        message: 'Re-indexed - submit again.',
        recommended_action: 'reindex',
      })
      queryClient.invalidateQueries({ queryKey: ['project-index', projectId] })
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
              onChange={e => {
                setFeature(e.target.value)
                // Editing the request invalidates a prior conflict verdict.
                setModeConflict(null)
              }}
              rows={5}
              className="resize-none"
            />
            <div className="grid gap-2">
              <Label>Mode</Label>
              <div
                role="radiogroup"
                aria-label="Run mode"
                className="grid gap-2"
              >
                {MODE_OPTIONS.map(option => {
                  const selected = requestedMode === option.value
                  const isSuggested =
                    suggestion?.suggested_mode === option.value
                  return (
                    <button
                      type="button"
                      key={option.value}
                      role="radio"
                      aria-checked={selected}
                      onClick={() => {
                        setRequestedMode(option.value)
                        // The user is now in control of the selection for this
                        // request: stop auto-selecting from suggestions.
                        userChangedModeRef.current = true
                        // Changing the selected mode clears a stale conflict.
                        setModeConflict(null)
                      }}
                      className={`flex items-start gap-3 rounded border px-3 py-2 text-left transition-colors ${
                        selected
                          ? 'border-primary bg-primary/5 ring-1 ring-primary'
                          : 'border-input hover:border-primary/50'
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                          selected ? 'border-primary' : 'border-muted-foreground'
                        }`}
                      >
                        {selected && (
                          <span className="h-2 w-2 rounded-full bg-primary" />
                        )}
                      </span>
                      <span className="min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="text-sm font-medium">
                            {option.title}
                          </span>
                          {isSuggested && (
                            <Badge variant="secondary" className="shrink-0">
                              Suggested
                            </Badge>
                          )}
                        </span>
                        <span className="block text-xs text-muted-foreground">
                          {option.copy}
                        </span>
                      </span>
                    </button>
                  )
                })}
              </div>
              {suggestion?.suggested_mode && suggestion.reason && (
                <p className="text-xs text-muted-foreground">
                  Suggested by Pipewright: {suggestion.reason} You can pick any
                  mode — your selection is what runs.
                </p>
              )}
            </div>
            {modeConflict && (
              <div className="rounded border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900">
                <p className="font-semibold">Confirm run mode</p>
                <p className="mt-1">{modeConflict.message}</p>
                <p className="mt-2 text-xs text-amber-800">
                  Confirming does not run code immediately. Pipewright will
                  create a chunk plan first, and code runs only after you approve
                  it.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {modeConflict.options.map((option) => (
                    <Button
                      key={`${option.mode}-${String(option.confirm_conflict)}`}
                      type="button"
                      size="sm"
                      variant={option.confirm_conflict ? 'default' : 'outline'}
                      disabled={runMutation.isPending}
                      onClick={() => chooseConflictOption(option)}
                    >
                      {option.confirm_conflict
                        ? `Continue with ${option.label}`
                        : `Switch to ${option.label}`}
                    </Button>
                  ))}
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={runMutation.isPending}
                    onClick={() => setModeConflict(null)}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
            {submitError && (
              <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                {submitError}
              </div>
            )}
            {noRunResponse && (
              <div className="grid gap-3 rounded border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-900">
                <div>
                  <p className="font-semibold">{noRunTitle(noRunResponse)}</p>
                  {noRunResponse.message && (
                    <p className="mt-1">{noRunResponse.message}</p>
                  )}
                  <p className="mt-1">{noRunGuidance(noRunResponse)}</p>
                </div>
                {isUnsafeStartBranchResponse(noRunResponse) && (
                  <div className="grid gap-1 text-xs text-amber-800 sm:grid-cols-2">
                    <div>
                      <span className="font-medium">Current branch: </span>
                      {noRunResponse.current_branch ?? 'detached HEAD'}
                    </div>
                    {noRunResponse.current_head_sha_short && (
                      <div>
                        <span className="font-medium">HEAD: </span>
                        {noRunResponse.current_head_sha_short}
                      </div>
                    )}
                  </div>
                )}
                {isStaleIndexResponse(noRunResponse) && (
                  <div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => reindexMutation.mutate()}
                      disabled={reindexMutation.isPending}
                    >
                      {reindexMutation.isPending
                        ? 'Re-indexing...'
                        : 'Re-index repository'}
                    </Button>
                  </div>
                )}
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
                {clarification.candidates &&
                  clarification.candidates.length > 0 &&
                  clarification.clarification_id && (
                    <div className="mt-3">
                      <p className="font-medium">Choose a file:</p>
                      <div className="mt-1 flex flex-col items-start gap-2">
                        {clarification.candidates.map((path) => (
                          <Button
                            key={path}
                            variant="outline"
                            size="sm"
                            disabled={selectMutation.isPending}
                            onClick={() => selectMutation.mutate(path)}
                          >
                            Use {path}
                            {path === clarification.recommended_path && (
                              <Badge variant="secondary" className="ml-2">
                                Recommended
                              </Badge>
                            )}
                          </Button>
                        ))}
                      </div>
                      <div className="mt-2 flex items-center gap-2">
                        <Input
                          value={selectionReply}
                          onChange={(e) => setSelectionReply(e.target.value)}
                          onKeyDown={(e) => {
                            if (
                              e.key === 'Enter' &&
                              selectionReply.trim() &&
                              !selectMutation.isPending
                            ) {
                              selectMutation.mutate(selectionReply)
                            }
                          }}
                          placeholder='Or reply, e.g. "1", "yes 1", "use README.md"'
                          disabled={selectMutation.isPending}
                          className="max-w-sm"
                        />
                        <Button
                          size="sm"
                          disabled={
                            !selectionReply.trim() || selectMutation.isPending
                          }
                          onClick={() => selectMutation.mutate(selectionReply)}
                        >
                          {selectMutation.isPending ? 'Selecting...' : 'Send'}
                        </Button>
                      </div>
                    </div>
                  )}
                {clarification.candidates &&
                  clarification.candidates.length > 0 && (
                    <p className="mt-3 text-xs text-amber-700">
                      This is based on the current repo index. Re-index if
                      recently added/removed files are missing.
                    </p>
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
              onClick={() => {
                // A fresh submission supersedes any prior conflict verdict.
                setModeConflict(null)
                runMutation.mutate({})
              }}
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
