export type StatusTone =
  | 'neutral'
  | 'running'
  | 'approval'
  | 'success'
  | 'danger'

interface StatusDisplay {
  label: string
  className: string
  style: {
    bg: string
    color: string
  }
}

const toneClasses: Record<StatusTone, string> = {
  neutral: 'border-gray-200 bg-gray-100 text-gray-700',
  running: 'border-blue-200 bg-blue-100 text-blue-700',
  approval: 'border-yellow-200 bg-yellow-100 text-yellow-800',
  success: 'border-green-200 bg-green-100 text-green-700',
  danger: 'border-red-200 bg-red-100 text-red-700',
}

const toneStyles: Record<StatusTone, { bg: string; color: string }> = {
  neutral: { bg: '#F3F4F6', color: '#374151' },
  running: { bg: '#DBEAFE', color: '#1D4ED8' },
  approval: { bg: '#FEF3C7', color: '#92400E' },
  success: { bg: '#D1FAE5', color: '#065F46' },
  danger: { bg: '#FEE2E2', color: '#991B1B' },
}

const statusTones: Record<string, StatusTone> = {
  pending: 'neutral',
  none: 'neutral',
  started: 'neutral',
  running: 'running',
  running_chunks: 'running',
  pushing: 'running',
  paused: 'approval',
  awaiting_approval: 'approval',
  awaiting_chunk_plan_approval: 'approval',
  awaiting_chunk_approval: 'approval',
  awaiting_final_approval: 'approval',
  chunk_plan_approved: 'success',
  chunk_approved: 'success',
  final_approved: 'success',
  approved: 'success',
  complete: 'success',
  completed: 'success',
  failed: 'danger',
  push_failed: 'danger',
  rejected: 'danger',
  final_rejected: 'danger',
}

export function formatStatusLabel(status: string) {
  return status.replace(/_/g, ' ').toUpperCase()
}

export function getStatusTone(status: string): StatusTone {
  return statusTones[status] ?? 'neutral'
}

export function getStatusDisplay(status: string): StatusDisplay {
  const tone = getStatusTone(status)

  return {
    label: formatStatusLabel(status),
    className: toneClasses[tone],
    style: toneStyles[tone],
  }
}

export function getApprovalTypeLabel(approvalType?: string | null) {
  if (approvalType === 'chunk') return 'Chunk Approval'
  if (approvalType === 'final') return 'Final Approval'
  return 'Legacy/Pre-merge Approval'
}
