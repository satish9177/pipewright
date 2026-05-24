import { useEffect, useRef } from 'react'
import type { ConnectionStatus, RunEvent } from '@/types/events'

interface Props {
  events: RunEvent[]
  status: ConnectionStatus
}

const statusConfig: Record<ConnectionStatus, {
  label: string
  dotClassName: string
}> = {
  live: {
    label: 'Live',
    dotClassName: 'bg-green-600',
  },
  reconnecting: {
    label: 'Reconnecting...',
    dotClassName: 'bg-yellow-500',
  },
  'polling-fallback': {
    label: 'Polling',
    dotClassName: 'bg-gray-400',
  },
  connecting: {
    label: 'Connecting...',
    dotClassName: 'bg-gray-400',
  },
}

function formatTime(ts: string) {
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return '--:--:--'
  return date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function eventLabel(event: RunEvent) {
  return event.stage ? `${event.stage}/${event.kind}` : event.kind
}

function eventMessage(event: RunEvent) {
  const toStatus = event.data.to_status
  if (
    (event.kind === 'run_status_changed' ||
      event.kind === 'chunk_status_changed') &&
    typeof toStatus === 'string' &&
    !event.message.includes(toStatus)
  ) {
    return `${event.message} (${toStatus})`
  }
  return event.message
}

export default function EventLog({ events, status }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const wasNearBottomRef = useRef(true)
  const visibleEvents = events.filter(event => event.kind !== 'heartbeat')
  const config = statusConfig[status]

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    if (wasNearBottomRef.current) {
      container.scrollTop = container.scrollHeight
    }
  }, [visibleEvents.length])

  const handleScroll = () => {
    const container = containerRef.current
    if (!container) return
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight
    wasNearBottomRef.current = distanceFromBottom < 32
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <p className="text-sm font-medium">Live Log</p>
        <div className="flex items-center gap-2 text-xs text-muted-foreground font-mono">
          <span className={`h-2 w-2 rounded-full ${config.dotClassName}`} />
          <span>{config.label}</span>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="max-h-80 overflow-y-auto font-mono text-xs border rounded bg-muted/30"
      >
        {visibleEvents.length === 0 ? (
          <div className="px-3 py-6 text-center text-muted-foreground">
            Waiting for run events...
          </div>
        ) : (
          <div className="divide-y">
            {visibleEvents.map(event => (
              <div
                key={event.id}
                className="grid grid-cols-[72px_160px_1fr] gap-3 px-3 py-2"
              >
                <span className="text-muted-foreground">
                  {formatTime(event.ts)}
                </span>
                <span className="truncate text-muted-foreground">
                  {eventLabel(event)}
                </span>
                <span className="break-words">
                  {eventMessage(event)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
