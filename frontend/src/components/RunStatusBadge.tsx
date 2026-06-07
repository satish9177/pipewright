import { getStatusDisplay, getFriendlyStatusLabel } from '@/utils/statusDisplay'

interface Props {
  status: string
  // #35C: opt-in friendly run-status label (e.g. "Needs your review"). Only the
  // Run Detail header passes this; other callers (run lists, approval-gate
  // statuses) keep the raw enum label. When friendly, the raw enum is preserved
  // in the tooltip so the technical value stays auditable.
  friendly?: boolean
}

export default function RunStatusBadge({ status, friendly = false }: Props) {
  const display = getStatusDisplay(status)
  const label = friendly ? getFriendlyStatusLabel(status) : display.label

  return (
    <span
      title={friendly ? display.label : undefined}
      style={{
        backgroundColor: display.style.bg,
        color: display.style.color,
        fontSize: 10,
        fontFamily: 'IBM Plex Mono, monospace',
        fontWeight: 500,
        padding: '2px 8px',
        borderRadius: 999,
        letterSpacing: '0.05em',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  )
}
