import { useState } from 'react'
import type { Run } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { getStatusDisplay } from '@/utils/statusDisplay'

interface FinalApprovalPanelProps {
  run: Run
  hasPendingFinalGate: boolean
  isCheckingFinalGate: boolean
  isApproving: boolean
  isRejecting: boolean
  message: string | null
  error: string | null
  onApprove: () => void
  onReject: (reason: string) => void
}

function getBooleanExtra(run: Run, field: string) {
  const value = run[field]
  return typeof value === 'boolean' ? value : null
}

export default function FinalApprovalPanel({
  run,
  hasPendingFinalGate,
  isCheckingFinalGate,
  isApproving,
  isRejecting,
  message,
  error,
  onApprove,
  onReject,
}: FinalApprovalPanelProps) {
  const [rejectReason, setRejectReason] = useState('')
  const actionPending =
    isApproving || isRejecting || isCheckingFinalGate || !hasPendingFinalGate
  const finalApprovalRequired = getBooleanExtra(run, 'final_approval_required')
  const statusDisplay = getStatusDisplay(run.status)

  return (
    <Card className="mb-4 border-yellow-400">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Final Approval</CardTitle>
            <CardDescription>
              Review the completed chunked run before allowing GitHub push and PR.
            </CardDescription>
          </div>
          <Badge variant="outline" className={statusDisplay.className}>
            {statusDisplay.label}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <p className="font-medium">Branch</p>
            <p className="text-muted-foreground break-words">
              {run.branch_name || 'Not created yet'}
            </p>
          </div>
          {finalApprovalRequired !== null && (
            <div>
              <p className="font-medium">Final Approval Required</p>
              <p className="text-muted-foreground">
                {finalApprovalRequired ? 'Yes' : 'No'}
              </p>
            </div>
          )}
        </div>

        <Textarea
          value={rejectReason}
          onChange={event => setRejectReason(event.target.value)}
          placeholder="Optional rejection reason"
          disabled={actionPending}
        />

        {!isCheckingFinalGate && !hasPendingFinalGate && (
          <p className="text-sm font-medium text-yellow-600">
            Run is awaiting final approval, but no pending final approval gate
            was found. Refresh the run or resume execution.
          </p>
        )}
        {message && (
          <p className="text-sm font-medium text-green-600">{message}</p>
        )}
        {error && (
          <p className="text-sm font-medium text-red-500">{error}</p>
        )}

        <div className="flex flex-wrap gap-3">
          <Button
            onClick={onApprove}
            disabled={actionPending}
            className="bg-green-600 hover:bg-green-700 text-white"
          >
            {isApproving ? 'Approving...' : 'Approve Final'}
          </Button>
          <Button
            variant="destructive"
            onClick={() => onReject(rejectReason)}
            disabled={actionPending}
          >
            {isRejecting ? 'Rejecting...' : 'Reject Final'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
