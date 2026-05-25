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

function eventSource(event: RunEvent) {
  if (typeof event.chunk_number === 'number') {
    return `chunk ${event.chunk_number}`
  }
  if (event.kind.includes('chunk')) return 'chunk'
  return 'run'
}

function eventSourceClass(event: RunEvent) {
  if (eventSource(event).startsWith('chunk')) {
    return 'border-yellow-200 bg-yellow-50 text-yellow-800'
  }
  return 'border-blue-200 bg-blue-50 text-blue-700'
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
    <div className="grid gap-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">Live Log</p>
          <p className="text-xs text-muted-foreground">
            Real-time run events from the backend event stream.
          </p>
        </div>
        <div className="flex items-center gap-2 rounded border bg-background px-2 py-1 text-xs text-muted-foreground font-mono">
          <span className={`h-2 w-2 rounded-full ${config.dotClassName}`} />
          <span>{config.label}</span>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="max-h-96 overflow-y-auto rounded border bg-background font-mono text-xs"
      >
        {visibleEvents.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <p className="font-medium text-foreground">No live events yet</p>
            <p className="mt-1 text-muted-foreground">
              Events will appear here when planning, approvals, execution, or
              PR actions start.
            </p>
          </div>
        ) : (
          <div className="divide-y">
            {visibleEvents.map(event => (
              <div
                key={event.id}
                className="grid gap-2 px-3 py-2 sm:grid-cols-[76px_88px_160px_1fr] sm:gap-3"
              >
                <span className="text-muted-foreground">
                  {formatTime(event.ts)}
                </span>
                <span
                  className={`w-fit rounded-full border px-2 py-0.5 text-[10px] uppercase ${eventSourceClass(event)}`}
                >
                  {eventSource(event)}
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
