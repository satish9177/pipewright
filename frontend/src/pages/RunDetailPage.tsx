import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { runsApi, gatesApi, projectsApi, ApprovalGate } from '@/api/client'
import type { ChunkPlanResponse, RunStatus } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import RunStatusBadge from '@/components/RunStatusBadge'
import EventLog from '@/components/EventLog'
import ChunkPlanPanel from '@/components/ChunkPlanPanel'
import FinalApprovalPanel from '@/components/FinalApprovalPanel'
import MemoryConflictPanel from '@/components/MemoryConflictPanel'
import PushPrPanel from '@/components/PushPrPanel'
import ReportView from '@/components/ReportView'
import PlanView from '@/components/PlanView'
import useRunEvents from '@/hooks/useRunEvents'

const STEPS = ['plan', 'code', 'patch', 'test', 'approval', 'github_pr']

function StepIndicator({
  currentStep,
  status,
}: {
  currentStep: string | null
  status: string
}) {
  const currentIndex = currentStep ? STEPS.indexOf(currentStep) : -1

  return (
    <div className="flex items-center gap-1 mb-6 flex-wrap">
      {STEPS.map((step, i) => {
        const isDone = status === 'complete' || i < currentIndex
        const isCurrent = step === currentStep && status !== 'complete'
        const isFailed = status === 'failed' && isCurrent

        return (
          <div key={step} className="flex items-center gap-1">
            <div
              className={`
                text-xs px-2 py-1 rounded font-medium
                ${isFailed
                  ? 'bg-red-500 text-white'
                  : isDone
                  ? 'bg-green-500 text-white'
                  : isCurrent
                  ? 'bg-blue-500 text-white'
                  : 'bg-muted text-muted-foreground'}
              `}
            >
              {step}
            </div>
            {i < STEPS.length - 1 && (
              <span className="text-muted-foreground text-xs">{'>'}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function getErrorMessage(error: unknown, fallback: string) {
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

  return fallback
}

function shouldPollRunStatus(status: RunStatus) {
  return [
    'running',
    'running_chunks',
    'paused',
    'awaiting_chunk_plan_approval',
    'awaiting_chunk_approval',
    'awaiting_final_approval',
    'awaiting_memory_conflict_approval',
    'pushing',
  ].includes(status)
}

function shouldPollChunkPlan(plan: ChunkPlanResponse) {
  if (plan.chunk_plan_status === 'awaiting_approval') return true
  if (plan.chunk_plan_status !== 'approved') return false

  return plan.chunks.some(chunk =>
    [
      'pending',
      'running',
      'failed',
      'awaiting_chunk_approval',
    ].includes(chunk.status)
  )
}

function shouldShowPushPrPanel(status: RunStatus, hasPrData: boolean) {
  return (
    hasPrData ||
    status === 'final_approved' ||
    status === 'pushing' ||
    status === 'push_failed'
  )
}

function isPendingFinalGate(gate: ApprovalGate, runId: string) {
  return (
    gate.run_id === runId &&
    gate.approval_type === 'final' &&
    gate.chunk_number === 0 &&
    gate.status === 'pending'
  )
}

function isPendingMemoryConflictGate(gate: ApprovalGate, runId: string) {
  return (
    gate.run_id === runId &&
    gate.approval_type === 'memory_conflict' &&
    gate.chunk_number === 0 &&
    gate.status === 'pending'
  )
}

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { events, status: wsStatus } = useRunEvents(runId)
  const [chunkPlanActionError, setChunkPlanActionError] = useState<string | null>(
    null
  )
  const [chunkExecutionMessage, setChunkExecutionMessage] = useState<
    string | null
  >(null)
  const [chunkExecutionError, setChunkExecutionError] = useState<string | null>(
    null
  )
  const [chunkActionMessage, setChunkActionMessage] = useState<string | null>(
    null
  )
  const [chunkActionError, setChunkActionError] = useState<string | null>(null)
  const [finalApprovalMessage, setFinalApprovalMessage] = useState<
    string | null
  >(null)
  const [finalApprovalError, setFinalApprovalError] = useState<string | null>(
    null
  )
  const [memoryConflictMessage, setMemoryConflictMessage] = useState<
    string | null
  >(null)
  const [memoryConflictError, setMemoryConflictError] = useState<string | null>(
    null
  )
  const [pushPrMessage, setPushPrMessage] = useState<string | null>(null)
  const [pushPrError, setPushPrError] = useState<string | null>(null)
  const [startImplementationError, setStartImplementationError] = useState<
    string | null
  >(null)

  const { data: run } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => runsApi.get(runId!),
    enabled: !!runId,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      return shouldPollRunStatus(data.status)
        ? 2000
        : false
    },
  })

  const { data: gates, isLoading: gatesLoading } = useQuery({
    queryKey: ['gates'],
    queryFn: gatesApi.list,
    refetchInterval: run?.status === 'paused' ? 2000 : false,
  })

  const { data: chunkPlan } = useQuery({
    queryKey: ['runChunks', runId],
    queryFn: () => runsApi.getRunChunks(runId!),
    enabled: !!runId,
    retry: false,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      return shouldPollChunkPlan(data) ? 2000 : false
    },
  })

  const { data: project } = useQuery({
    queryKey: ['project', run?.project_id],
    queryFn: () => projectsApi.get(run!.project_id!),
    enabled: !!run?.project_id,
  })

  const pendingFinalGate = gates?.find(
    (gate: ApprovalGate) => runId ? isPendingFinalGate(gate, runId) : false
  )

  const pendingMemoryConflictGate = gates?.find(
    (gate: ApprovalGate) =>
      runId ? isPendingMemoryConflictGate(gate, runId) : false
  )

  const pendingGate = gates?.find(
    (g: ApprovalGate) =>
      g.run_id === runId &&
      g.status === 'pending' &&
      !isPendingFinalGate(g, runId!) &&
      !isPendingMemoryConflictGate(g, runId!)
  )

  const approveMutation = useMutation({
    mutationFn: () => gatesApi.approve(pendingGate!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
    },
  })

  const rejectMutation = useMutation({
    mutationFn: () => gatesApi.reject(pendingGate!.id, 'Rejected by user'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
    },
  })

  const approveChunkPlanMutation = useMutation({
    mutationFn: () => runsApi.approveChunkPlan(runId!),
    onSuccess: () => {
      setChunkPlanActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkPlanActionError(
        getErrorMessage(error, 'Failed to approve chunk plan.')
      )
    },
  })

  const rejectChunkPlanMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectChunkPlan(runId!, reason || 'Chunk plan rejected by user'),
    onSuccess: () => {
      setChunkPlanActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkPlanActionError(
        getErrorMessage(error, 'Failed to reject chunk plan.')
      )
    },
  })

  const executeChunksMutation = useMutation({
    mutationFn: () => runsApi.executeChunks(runId!),
    onSuccess: (response) => {
      setChunkExecutionMessage(
        response.message || 'Chunk execution started.'
      )
      setChunkExecutionError(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkExecutionMessage(null)
      setChunkExecutionError(
        getErrorMessage(error, 'Failed to execute chunks.')
      )
    },
  })

  const resumeChunksMutation = useMutation({
    mutationFn: () => runsApi.resumeChunks(runId!),
    onSuccess: (response) => {
      setChunkExecutionMessage(response.message || 'Chunked run resumed.')
      setChunkExecutionError(null)
      setChunkActionMessage(null)
      setChunkActionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkExecutionMessage(null)
      setChunkExecutionError(getErrorMessage(error, 'Failed to resume run.'))
    },
  })

  const approveChunkMutation = useMutation({
    mutationFn: (chunkNumber: number) => runsApi.approveChunk(runId!, chunkNumber),
    onSuccess: (response, chunkNumber) => {
      setChunkActionMessage(
        response.message || `Chunk ${chunkNumber} approved.`
      )
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getErrorMessage(error, 'Failed to approve chunk.'))
    },
  })

  const rejectChunkMutation = useMutation({
    mutationFn: ({
      chunkNumber,
      reason,
    }: {
      chunkNumber: number
      reason: string
    }) =>
      runsApi.rejectChunk(
        runId!,
        chunkNumber,
        reason || 'Chunk rejected by user'
      ),
    onSuccess: (response, variables) => {
      setChunkActionMessage(
        response.message || `Chunk ${variables.chunkNumber} rejected.`
      )
      setChunkActionError(null)
      setChunkExecutionMessage(null)
      setChunkExecutionError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setChunkActionMessage(null)
      setChunkActionError(getErrorMessage(error, 'Failed to reject chunk.'))
    },
  })

  const approveFinalApprovalMutation = useMutation({
    mutationFn: () => runsApi.approveFinalApproval(runId!),
    onSuccess: () => {
      setFinalApprovalMessage('Final approval accepted.')
      setFinalApprovalError(null)
      setPushPrMessage(null)
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setFinalApprovalMessage(null)
      setFinalApprovalError(
        getErrorMessage(error, 'Failed to approve final approval.')
      )
    },
  })

  const rejectFinalApprovalMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectFinalApproval(
        runId!,
        reason || 'Final approval rejected by user'
      ),
    onSuccess: () => {
      setFinalApprovalMessage('Final approval rejected.')
      setFinalApprovalError(null)
      setPushPrMessage(null)
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setFinalApprovalMessage(null)
      setFinalApprovalError(
        getErrorMessage(error, 'Failed to reject final approval.')
      )
    },
  })

  const approveMemoryConflictMutation = useMutation({
    mutationFn: () => runsApi.approveMemoryConflict(runId!),
    onSuccess: () => {
      setMemoryConflictMessage(
        'Conflict overridden for this run. Continuing execution...'
      )
      setMemoryConflictError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
      // Override-once: the run returned to an executable state. Re-trigger
      // execution so the now-honored override lets the run continue.
      executeChunksMutation.mutate()
    },
    onError: (error: unknown) => {
      setMemoryConflictMessage(null)
      setMemoryConflictError(
        getErrorMessage(error, 'Failed to override memory conflict.')
      )
    },
  })

  const rejectMemoryConflictMutation = useMutation({
    mutationFn: (reason: string) =>
      runsApi.rejectMemoryConflict(
        runId!,
        reason || 'Memory conflict rejected by user'
      ),
    onSuccess: () => {
      setMemoryConflictMessage('Run rejected due to memory conflict.')
      setMemoryConflictError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setMemoryConflictMessage(null)
      setMemoryConflictError(
        getErrorMessage(error, 'Failed to reject memory conflict.')
      )
    },
  })

  const pushPrMutation = useMutation({
    mutationFn: () => runsApi.pushPr(runId!),
    onSuccess: (response) => {
      setPushPrMessage(
        response.pr_url
          ? `Pull request created: ${response.pr_url}`
          : 'Push/PR operation completed.'
      )
      setPushPrError(null)
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runChunks', runId] })
      queryClient.invalidateQueries({ queryKey: ['gates'] })
    },
    onError: (error: unknown) => {
      setPushPrMessage(null)
      setPushPrError(getErrorMessage(error, 'Failed to push/create PR.'))
    },
  })

  const startImplementationMutation = useMutation({
    mutationFn: () => runsApi.startImplementation(runId!),
    onSuccess: (response) => {
      setStartImplementationError(null)
      navigate(`/runs/${response.run_id}`)
    },
    onError: (error: unknown) => {
      setStartImplementationError(
        getErrorMessage(error, 'Failed to start implementation.'),
      )
    },
  })

  if (!run) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading run...</p>
      </div>
    )
  }

  if (run.intent === 'report_only' || run.status === 'report_ready') {
    return <ReportView run={run} onBack={() => navigate(-1)} />
  }

  if (run.intent === 'plan_only' || run.status === 'plan_ready') {
    return (
      <PlanView
        run={run}
        chunkPlan={chunkPlan}
        onBack={() => navigate(-1)}
        onStartImplementation={() => startImplementationMutation.mutate()}
        isStartingImplementation={startImplementationMutation.isPending}
        startImplementationError={startImplementationError}
      />
    )
  }

  const hasPrData = Boolean(run.pr_url || run.pr_number || run.push_error)
  const showMemoryConflictPanel =
    run.status === 'awaiting_memory_conflict_approval'
  const showFinalApprovalPanel = run.status === 'awaiting_final_approval'
  const showPushPrPanel = shouldShowPushPrPanel(run.status, hasPrData)

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Pipeline Run</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {run.id}
          </p>
        </div>
        <RunStatusBadge status={run.status} />
      </div>

      <Card className="mb-6 border-muted-foreground/20">
        <CardHeader>
          <CardTitle className="text-base">Run Summary</CardTitle>
          <CardDescription>
            Current state, requested feature, and pipeline progress.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-3 text-sm sm:grid-cols-3">
            <div>
              <p className="font-medium">Current Step</p>
              <p className="text-muted-foreground">
                {run.current_step || 'Not started'}
              </p>
            </div>
            <div>
              <p className="font-medium">Total Chunks</p>
              <p className="text-muted-foreground">
                {run.total_chunks ?? 'Unknown'}
              </p>
            </div>
            <div>
              <p className="font-medium">Current Chunk</p>
              <p className="text-muted-foreground">
                {run.current_chunk_number ?? 'None'}
              </p>
            </div>
          </div>
          <div>
            <p className="text-sm font-medium mb-1">Feature</p>
            <p className="text-sm text-muted-foreground whitespace-pre-wrap">
              {run.feature_description}
            </p>
          </div>
          <StepIndicator
            currentStep={run.current_step}
            status={run.status}
          />
        </CardContent>
      </Card>

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="text-sm font-semibold">Chunk Plan and Execution</h3>
          <p className="text-xs text-muted-foreground">
            Review the plan, execute approved chunks, and resolve chunk-level
            approvals.
          </p>
        </div>

        {chunkPlan ? (
          <ChunkPlanPanel
            plan={chunkPlan}
            isApproving={approveChunkPlanMutation.isPending}
            isRejecting={rejectChunkPlanMutation.isPending}
            isExecuting={executeChunksMutation.isPending}
            isResuming={resumeChunksMutation.isPending}
            approvingChunkNumber={approveChunkMutation.variables ?? null}
            rejectingChunkNumber={
              rejectChunkMutation.variables?.chunkNumber ?? null
            }
            error={chunkPlanActionError}
            executionMessage={chunkExecutionMessage}
            executionError={chunkExecutionError}
            chunkActionMessage={chunkActionMessage}
            chunkActionError={chunkActionError}
            onApprove={() => approveChunkPlanMutation.mutate()}
            onReject={(reason) => rejectChunkPlanMutation.mutate(reason)}
            onExecute={() => executeChunksMutation.mutate()}
            onResume={() => resumeChunksMutation.mutate()}
            onApproveChunk={(chunkNumber) =>
              approveChunkMutation.mutate(chunkNumber)
            }
            onRejectChunk={(chunkNumber, reason) =>
              rejectChunkMutation.mutate({ chunkNumber, reason })
            }
          />
        ) : (
          <Card className="mb-4 border-dashed">
            <CardContent className="py-6">
              <p className="text-sm font-medium">No chunk plan loaded</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Legacy runs or interrupted runs may not have chunk plan data.
              </p>
            </CardContent>
          </Card>
        )}
      </section>

      {pendingGate && (
        <Card className="mb-4 border-yellow-400">
          <CardHeader>
            <CardTitle className="text-base">
              Human Approval Required
            </CardTitle>
            <CardDescription>
              Review the changes and approve or reject
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">Risk:</span>
              <span
                className={`text-sm font-medium ${
                  pendingGate.risk_level === 'high'
                    ? 'text-red-500'
                    : pendingGate.risk_level === 'medium'
                    ? 'text-yellow-500'
                    : 'text-green-500'
                }`}
              >
                {pendingGate.risk_level}
              </span>
            </div>

            {pendingGate.ai_summary && (
              <div>
                <p className="text-sm font-medium mb-2">Summary</p>
                <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                  {pendingGate.ai_summary}
                </p>
              </div>
            )}

            <Separator />

            {pendingGate.diff && (
              <div>
                <p className="text-sm font-medium mb-2">Diff</p>
                <pre className="text-xs bg-muted p-3 rounded overflow-auto max-h-64 whitespace-pre-wrap font-mono">
                  {pendingGate.diff}
                </pre>
              </div>
            )}

            <Separator />

            <div className="flex gap-3">
              <Button
                onClick={() => approveMutation.mutate()}
                disabled={approveMutation.isPending}
                className="bg-green-600 hover:bg-green-700 text-white"
              >
                {approveMutation.isPending
                  ? 'Approving...'
                  : 'Approve'}
              </Button>
              <Button
                variant="destructive"
                onClick={() => rejectMutation.mutate()}
                disabled={rejectMutation.isPending}
              >
                {rejectMutation.isPending
                  ? 'Rejecting...'
                  : 'Reject'}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {showMemoryConflictPanel && (
        <section className="mb-6">
          <div className="mb-3">
            <h3 className="text-sm font-semibold">Memory Conflict</h3>
            <p className="text-xs text-muted-foreground">
              A DB memory conflict paused this run. Override once to continue or
              reject the run.
            </p>
          </div>
          <MemoryConflictPanel
            gate={pendingMemoryConflictGate}
            isCheckingGate={gatesLoading}
            isApproving={approveMemoryConflictMutation.isPending}
            isRejecting={rejectMemoryConflictMutation.isPending}
            message={memoryConflictMessage}
            error={memoryConflictError}
            onApprove={() => approveMemoryConflictMutation.mutate()}
            onReject={(reason) => rejectMemoryConflictMutation.mutate(reason)}
          />
        </section>
      )}

      {(showFinalApprovalPanel || showPushPrPanel) && (
        <section className="mb-6">
          <div className="mb-3">
            <h3 className="text-sm font-semibold">Final Approval and PR</h3>
            <p className="text-xs text-muted-foreground">
              Complete the human approval loop, then push and create a GitHub PR.
            </p>
          </div>

          {showFinalApprovalPanel && (
            <FinalApprovalPanel
              run={run}
              hasPendingFinalGate={Boolean(pendingFinalGate)}
              isCheckingFinalGate={gatesLoading}
              isApproving={approveFinalApprovalMutation.isPending}
              isRejecting={rejectFinalApprovalMutation.isPending}
              message={finalApprovalMessage}
              error={finalApprovalError}
              onApprove={() => approveFinalApprovalMutation.mutate()}
              onReject={(reason) => rejectFinalApprovalMutation.mutate(reason)}
            />
          )}

          {showPushPrPanel && (
            <PushPrPanel
              run={run}
              project={project}
              isPushing={pushPrMutation.isPending}
              message={pushPrMessage}
              error={pushPrError}
              onPush={() => pushPrMutation.mutate()}
            />
          )}
        </section>
      )}

      {run.status === 'complete' && (
        <Card className="mb-4 border-green-500">
          <CardContent className="py-4">
            {run.current_step === 'local_only_complete' ? (
              <>
                <p className="text-sm font-medium text-green-600">
                  Pipeline completed locally.
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  No GitHub PR was created because this project is in Local-only
                  mode. Your changes were committed to the local Pipewright
                  branch
                  {run.branch_name ? ` (${run.branch_name})` : ''}. Push the
                  branch manually if you want to open a PR.
                </p>
              </>
            ) : run.pr_url ? (
              <>
                <p className="text-sm font-medium text-green-600">
                  Pipeline completed successfully.
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Check GitHub for the Pull Request:{' '}
                  <a
                    href={run.pr_url}
                    target="_blank"
                    rel="noreferrer"
                    className="underline"
                  >
                    {run.pr_url}
                  </a>
                </p>
              </>
            ) : (
              <p className="text-sm font-medium text-green-600">
                Pipeline completed successfully.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {run.status === 'failed' && (
        <Card className="mb-4 border-red-500">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-red-500">
              Pipeline failed at step: {run.current_step}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Check the terminal for error details.
            </p>
          </CardContent>
        </Card>
      )}

      {(run.status === 'rejected' || run.status === 'final_rejected') && (
        <Card className="mb-4 border-gray-400">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-gray-500">
              Pipeline was rejected. Files have been rolled back.
            </p>
          </CardContent>
        </Card>
      )}

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="text-sm font-semibold">Timeline</h3>
          <p className="text-xs text-muted-foreground">
            Live run events and status changes from the backend.
          </p>
        </div>
        <Card>
          <CardContent className="py-4">
            <EventLog events={events} status={wsStatus} />
          </CardContent>
        </Card>
      </section>

      <div className="mt-4">
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate(-1)}
        >
          Back
        </Button>
      </div>
    </div>
  )
}
