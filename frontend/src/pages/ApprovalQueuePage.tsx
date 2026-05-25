import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { gatesApi, ApprovalGate } from '@/api/client'
import RunStatusBadge from '@/components/RunStatusBadge'
import { getApprovalTypeLabel } from '@/utils/statusDisplay'

function getGateSubtitle(gate: ApprovalGate) {
  if (gate.approval_type === 'chunk' && gate.chunk_number) {
    return `Chunk ${gate.chunk_number}`
  }
  if (gate.approval_type === 'final') {
    return 'Final review'
  }
  return gate.step || 'Pre-merge review'
}

export default function ApprovalQueuePage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data: gates, isLoading } = useQuery({
    queryKey: ['gates'],
    queryFn: gatesApi.list,
    refetchInterval: 3000,
  })

  const pending = gates?.filter((g: ApprovalGate) => g.status === 'pending') ?? []

  const approveMutation = useMutation({
    mutationFn: (gateId: string) => gatesApi.approve(gateId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['gates'] }),
  })

  const rejectMutation = useMutation({
    mutationFn: (gateId: string) => gatesApi.reject(gateId, 'Rejected'),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['gates'] }),
  })

  return (
    <div style={{ padding: '24px 28px', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ marginBottom: 24 }}>
        <div style={{
          fontSize: 10,
          fontFamily: 'IBM Plex Mono, monospace',
          letterSpacing: '0.08em',
          color: '#B7531C',
          textTransform: 'uppercase',
          marginBottom: 4,
        }}>
          APPROVAL QUEUE
        </div>
        <h1 style={{
          fontFamily: 'IBM Plex Sans, sans-serif',
          fontSize: 28,
          fontWeight: 600,
          margin: 0,
          color: '#0E1116',
        }}>
          Pending approvals
        </h1>
      </div>

      {isLoading && (
        <p style={{ color: '#6B7280', fontSize: 13 }}>Loading...</p>
      )}

      {pending.length === 0 && !isLoading && (
        <div style={{
          border: '1px dashed #D6D9DE',
          borderRadius: 4,
          padding: '48px 24px',
          textAlign: 'center',
        }}>
          <p style={{
            color: '#6B7280',
            fontSize: 13,
            fontFamily: 'IBM Plex Mono, monospace',
          }}>
            No pending approvals. Pipeline is clear.
          </p>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {pending.map((gate: ApprovalGate) => (
          <div key={gate.id} style={{
            border: '1px solid #B7531C',
            borderRadius: 4,
            backgroundColor: '#FAF8F4',
            overflow: 'hidden',
          }}>
            <div style={{
              padding: '12px 16px',
              borderBottom: '1px solid #D6D9DE',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
            }}>
              <div>
                <span style={{
                  fontSize: 10,
                  fontFamily: 'IBM Plex Mono, monospace',
                  letterSpacing: '0.06em',
                  color: '#B7531C',
                  textTransform: 'uppercase',
                }}>
                  {getApprovalTypeLabel(gate.approval_type)}
                </span>
                <p style={{
                  margin: '4px 0 0',
                  fontSize: 13,
                  fontFamily: 'IBM Plex Sans, sans-serif',
                  color: '#0E1116',
                }}>
                  Run: {gate.run_id.slice(0, 8)}...
                </p>
                <p style={{
                  margin: '4px 0 0',
                  fontSize: 12,
                  fontFamily: 'IBM Plex Sans, sans-serif',
                  color: '#6B7280',
                }}>
                  {getGateSubtitle(gate)}
                </p>
              </div>
              <div style={{
                display: 'flex',
                gap: 8,
                alignItems: 'center',
                flexWrap: 'wrap',
                justifyContent: 'flex-end',
              }}>
                <RunStatusBadge status={gate.status} />
                <button
                  onClick={() => navigate(`/runs/${gate.run_id}`)}
                  style={{
                    border: '1px solid #D6D9DE',
                    backgroundColor: 'transparent',
                    padding: '6px 12px',
                    borderRadius: 2,
                    fontSize: 12,
                    fontFamily: 'IBM Plex Sans, sans-serif',
                    cursor: 'pointer',
                    color: '#374151',
                  }}
                >
                  View run
                </button>
                <button
                  onClick={() => rejectMutation.mutate(gate.id)}
                  disabled={rejectMutation.isPending}
                  style={{
                    border: '1px solid #FCA5A5',
                    backgroundColor: '#FEE2E2',
                    padding: '6px 12px',
                    borderRadius: 2,
                    fontSize: 12,
                    fontFamily: 'IBM Plex Sans, sans-serif',
                    cursor: 'pointer',
                    color: '#991B1B',
                  }}
                >
                  Reject
                </button>
                <button
                  onClick={() => approveMutation.mutate(gate.id)}
                  disabled={approveMutation.isPending}
                  style={{
                    backgroundColor: '#B7531C',
                    border: 'none',
                    padding: '6px 14px',
                    borderRadius: 2,
                    fontSize: 12,
                    fontFamily: 'IBM Plex Sans, sans-serif',
                    cursor: 'pointer',
                    color: 'white',
                    fontWeight: 500,
                  }}
                >
                  Approve
                </button>
              </div>
            </div>

            {gate.ai_summary && (
              <div style={{ padding: '12px 16px' }}>
                <p style={{
                  fontSize: 12,
                  color: '#374151',
                  fontFamily: 'IBM Plex Sans, sans-serif',
                  margin: 0,
                }}>
                  {gate.ai_summary.slice(0, 300)}
                  {gate.ai_summary.length > 300 ? '...' : ''}
                </p>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
