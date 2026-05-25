import { getStatusDisplay } from '@/utils/statusDisplay'

interface Props {
  status: string
}

export default function RunStatusBadge({ status }: Props) {
  const display = getStatusDisplay(status)

  return (
    <span style={{
      backgroundColor: display.style.bg,
      color: display.style.color,
      fontSize: 10,
      fontFamily: 'IBM Plex Mono, monospace',
      fontWeight: 500,
      padding: '2px 8px',
      borderRadius: 999,
      letterSpacing: '0.05em',
      whiteSpace: 'nowrap',
    }}>
      {display.label}
    </span>
  )
}
