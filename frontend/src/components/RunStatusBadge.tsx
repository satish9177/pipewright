interface Props {
  status: string
}

const statusStyles: Record<string, {
  bg: string
  color: string
  label: string
}> = {
  running: { bg: '#DBEAFE', color: '#1D4ED8', label: 'RUNNING' },
  paused: { bg: '#FEF3C7', color: '#92400E', label: 'PAUSED' },
  complete: { bg: '#D1FAE5', color: '#065F46', label: 'COMPLETE' },
  failed: { bg: '#FEE2E2', color: '#991B1B', label: 'FAILED' },
  rejected: { bg: '#F3F4F6', color: '#374151', label: 'REJECTED' },
}

export default function RunStatusBadge({ status }: Props) {
  const style = statusStyles[status] ?? {
    bg: '#F3F4F6',
    color: '#374151',
    label: status.toUpperCase()
  }

  return (
    <span style={{
      backgroundColor: style.bg,
      color: style.color,
      fontSize: 10,
      fontFamily: 'IBM Plex Mono, monospace',
      fontWeight: 500,
      padding: '2px 8px',
      borderRadius: 999,
      letterSpacing: '0.05em',
      whiteSpace: 'nowrap',
    }}>
      {style.label}
    </span>
  )
}
