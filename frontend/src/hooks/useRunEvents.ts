import { useEffect, useRef, useState } from 'react'
import type {
  ConnectionStatus,
  RunEvent,
  RunEventSocketMessage,
} from '@/types/events'

const BASE_WS_URL = 'ws://localhost:8001'
const BASE_RETRY_MS = 1000
const MAX_RETRY_MS = 30000
const MAX_RETRIES = 20
const EVENT_LIMIT = 500

function buildUrl(runId: string, lastSeenEventId: string | null) {
  const base = `${BASE_WS_URL}/ws/runs/${runId}/events`
  if (!lastSeenEventId) return base
  return `${base}?since=${encodeURIComponent(lastSeenEventId)}`
}

function nextRetryDelay(retryCount: number) {
  return Math.min(BASE_RETRY_MS * 2 ** retryCount, MAX_RETRY_MS)
}

export default function useRunEvents(runId: string | undefined): {
  events: RunEvent[]
  status: ConnectionStatus
} {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [status, setStatus] = useState<ConnectionStatus>(
    runId ? 'connecting' : 'polling-fallback',
  )

  const socketRef = useRef<WebSocket | null>(null)
  const retryTimerRef = useRef<number | null>(null)
  const retryCountRef = useRef(0)
  const lastSeenEventIdRef = useRef<string | null>(null)
  const eventIdsRef = useRef<Set<string>>(new Set())
  const shouldReconnectRef = useRef(true)
  const mountedRef = useRef(false)
  const terminalRef = useRef(false)

  useEffect(() => {
    mountedRef.current = true
    shouldReconnectRef.current = true
    terminalRef.current = false
    retryCountRef.current = 0
    lastSeenEventIdRef.current = null
    eventIdsRef.current = new Set()
    setEvents([])

    if (!runId || typeof WebSocket === 'undefined') {
      setStatus('polling-fallback')
      return () => {
        mountedRef.current = false
      }
    }

    let activeSocket: WebSocket | null = null

    const clearRetryTimer = () => {
      if (retryTimerRef.current !== null) {
        window.clearTimeout(retryTimerRef.current)
        retryTimerRef.current = null
      }
    }

    const scheduleReconnect = () => {
      if (!mountedRef.current || !shouldReconnectRef.current) return
      if (terminalRef.current) {
        setStatus('polling-fallback')
        return
      }
      if (retryCountRef.current >= MAX_RETRIES) {
        setStatus('polling-fallback')
        return
      }

      const retryCount = retryCountRef.current
      retryCountRef.current += 1
      setStatus('reconnecting')
      retryTimerRef.current = window.setTimeout(() => {
        connect()
      }, nextRetryDelay(retryCount))
    }

    const appendEvent = (event: RunEvent) => {
      if (eventIdsRef.current.has(event.id)) return

      eventIdsRef.current.add(event.id)
      lastSeenEventIdRef.current = event.id
      setEvents(previous => {
        const next = [...previous, event].slice(-EVENT_LIMIT)
        const retainedIds = new Set(next.map(item => item.id))
        eventIdsRef.current = retainedIds
        return next
      })
    }

    const handleMessage = (message: RunEventSocketMessage) => {
      if (message.type === 'event') {
        appendEvent(message.event)
        return
      }

      if (message.type === 'replay_complete') {
        if (message.last_event_id) {
          lastSeenEventIdRef.current = message.last_event_id
        }
        return
      }

      if (message.type === 'close' && message.reason === 'run_terminal') {
        terminalRef.current = true
        shouldReconnectRef.current = false
        setStatus('polling-fallback')
        socketRef.current?.close(1000)
      }
    }

    function connect() {
      if (!mountedRef.current || !runId || terminalRef.current) return

      setStatus(retryCountRef.current > 0 ? 'reconnecting' : 'connecting')
      const socket = new WebSocket(buildUrl(runId, lastSeenEventIdRef.current))
      activeSocket = socket
      socketRef.current = socket

      socket.onopen = () => {
        if (!mountedRef.current) return
        retryCountRef.current = 0
        setStatus('live')
      }

      socket.onmessage = event => {
        if (!mountedRef.current) return
        try {
          handleMessage(JSON.parse(event.data) as RunEventSocketMessage)
        } catch {
          // Ignore malformed observability messages.
        }
      }

      socket.onerror = () => {
        if (!mountedRef.current) return
        setStatus('reconnecting')
      }

      socket.onclose = closeEvent => {
        if (!mountedRef.current) return
        if (!shouldReconnectRef.current || terminalRef.current || closeEvent.wasClean) {
          setStatus('polling-fallback')
          return
        }
        scheduleReconnect()
      }
    }

    connect()

    return () => {
      mountedRef.current = false
      shouldReconnectRef.current = false
      clearRetryTimer()
      if (activeSocket && activeSocket.readyState < WebSocket.CLOSING) {
        activeSocket.close(1000)
      }
      socketRef.current = null
    }
  }, [runId])

  return { events, status }
}
