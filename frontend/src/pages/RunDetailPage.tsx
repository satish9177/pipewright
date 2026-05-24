import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { runsApi, gatesApi, ApprovalGate } from '@/api/client'
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
import useRunEvents from '@/hooks/useRunEvents'

const STEPS = ['plan', 'code', 'patch', 'test', 'approval', 'github_pr']

function StepIndicator({
  currentStep,
  status,
}: {
  currentStep: string
  status: string
}) {
  const currentIndex = STEPS.indexOf(currentStep)

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

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { events, status: wsStatus } = useRunEvents(runId)

  const { data: run } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => runsApi.get(runId!),
    enabled: !!runId,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      return data.status === 'running' || data.status === 'paused'
        ? 2000
        : false
    },
  })

  const { data: gates } = useQuery({
    queryKey: ['gates'],
    queryFn: gatesApi.list,
    refetchInterval: run?.status === 'paused' ? 2000 : false,
  })

  const pendingGate = gates?.find(
    (g: ApprovalGate) => g.run_id === runId && g.status === 'pending'
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

  if (!run) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading run...</p>
      </div>
    )
  }

  const showEventLog =
    run.status === 'running' ||
    run.status === 'awaiting_chunk_approval' ||
    run.status === 'awaiting_final_approval' ||
    events.length > 0

  return (
    <div className="max-w-3xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">Pipeline Run</h2>
          <p className="text-xs text-muted-foreground font-mono mt-1">
            {run.id}
          </p>
        </div>
        <RunStatusBadge status={run.status} />
      </div>

      <Card className="mb-4">
        <CardContent className="py-4">
          <p className="text-sm font-medium mb-1">Feature</p>
          <p className="text-sm text-muted-foreground">
            {run.feature_description}
          </p>
        </CardContent>
      </Card>

      <StepIndicator
        currentStep={run.current_step}
        status={run.status}
      />

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

      {run.status === 'complete' && (
        <Card className="mb-4 border-green-500">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-green-600">
              Pipeline completed successfully.
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Check GitHub for the Pull Request.
            </p>
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

      {run.status === 'rejected' && (
        <Card className="mb-4 border-gray-400">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-gray-500">
              Pipeline was rejected. Files have been rolled back.
            </p>
          </CardContent>
        </Card>
      )}

      {showEventLog && (
        <Card className="mb-4">
          <CardContent className="py-4">
            <EventLog events={events} status={wsStatus} />
          </CardContent>
        </Card>
      )}

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
