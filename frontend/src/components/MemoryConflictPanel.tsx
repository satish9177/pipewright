import { useState } from 'react'
import type { ApprovalGate } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'

interface MemoryConflictPanelProps {
  gate?: ApprovalGate
  isCheckingGate: boolean
  isApproving: boolean
  isRejecting: boolean
  message: string | null
  error: string | null
  onApprove: () => void
  onReject: (reason: string) => void
}

export default function MemoryConflictPanel({
  gate,
  isCheckingGate,
  isApproving,
  isRejecting,
  message,
  error,
  onApprove,
  onReject,
}: MemoryConflictPanelProps) {
  const [rejectReason, setRejectReason] = useState('')
  const hasPendingGate = Boolean(gate)
  const actionPending =
    isApproving || isRejecting || isCheckingGate || !hasPendingGate
  const blockMessage = gate?.ai_summary || gate?.plain_english_summary || ''

  return (
    <Card className="mb-4 border-yellow-400">
      <CardHeader>
        <CardTitle className="text-base">DB Memory Conflict</CardTitle>
        <CardDescription>
          This run was paused because project memory disagrees with the
          repository&apos;s database, and the run touches DB/model/migration
          files. Resolve the stale memory, or override once to continue.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {blockMessage && (
          <pre className="whitespace-pre-wrap rounded-md bg-muted p-3 text-sm">
            {blockMessage}
          </pre>
        )}

        <Textarea
          value={rejectReason}
          onChange={event => setRejectReason(event.target.value)}
          placeholder="Optional rejection reason"
          disabled={actionPending}
        />

        {!isCheckingGate && !hasPendingGate && (
          <p className="text-sm font-medium text-yellow-600">
            Run is awaiting memory-conflict approval, but no pending gate was
            found. Refresh the run.
          </p>
        )}
        {message && (
          <p className="text-sm font-medium text-green-600">{message}</p>
        )}
        {error && <p className="text-sm font-medium text-red-500">{error}</p>}

        <div className="flex flex-wrap gap-3">
          <Button
            onClick={onApprove}
            disabled={actionPending}
            className="bg-green-600 hover:bg-green-700 text-white"
          >
            {isApproving ? 'Overriding...' : 'Override Once and Continue'}
          </Button>
          <Button
            variant="destructive"
            onClick={() => onReject(rejectReason)}
            disabled={actionPending}
          >
            {isRejecting ? 'Rejecting...' : 'Reject Run'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
