import { useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  memoryApi,
  type MemoryCategory,
  type MemoryCreateRequest,
  type MemoryFact,
  type MemoryPreviewRole,
  type MemoryScope,
  type MemorySuggestion,
  type MemorySuggestionStatus,
  type MemoryStatus,
  type MemoryUpdateRequest,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import MemoryAttentionPanel from '@/components/MemoryAttentionPanel'
import MemoryTrustStrip from '@/components/MemoryTrustStrip'
import { Textarea } from '@/components/ui/textarea'
import { getMemoryStatusDisplay } from '@/utils/memoryStatusDisplay'
import { buildMemoryTrustSummary } from '@/utils/memoryTrustSummary'

const CATEGORIES: MemoryCategory[] = [
  'stack',
  'structure',
  'test',
  'db',
  'style',
  'security',
  'architecture',
  'deploy',
  'forbidden_paths',
  'reviewer_pref',
  'other',
]

const SCOPES: MemoryScope[] = ['global', 'backend', 'frontend', 'tests', 'infra']
const STATUSES: MemoryStatus[] = ['active', 'stale', 'archived', 'historical']
const SUGGESTION_STATUSES: MemorySuggestionStatus[] = [
  'pending',
  'approved',
  'rejected',
  'archived',
]
const ROLES: MemoryPreviewRole[] = [
  'triage',
  'planner',
  'architect',
  'coder',
  'reviewer',
  'summary',
]

const STATUS_ORDER: Record<string, number> = {
  active: 0,
  stale: 1,
  archived: 2,
  historical: 3,
}

const SUGGESTION_STATUS_ORDER: Record<string, number> = {
  pending: 0,
  approved: 1,
  rejected: 2,
  archived: 3,
}

interface ProjectMemoryPanelProps {
  projectId: string
}

interface MemoryFormState {
  content: string
  category: MemoryCategory
  scope: MemoryScope
  priority: string
}

interface MarkStaleDialogState {
  fact: MemoryFact
  reason: string
  error: string | null
}

interface RetireDialogState {
  fact: MemoryFact
  reason: string
  error: string | null
}

interface SupersedeDialogState {
  oldFact: MemoryFact
  newFactId: string
  reason: string
  error: string | null
}

interface ApproveSupersedeDialogState {
  suggestion: MemorySuggestion
  oldFactId: string
  editedContent: string
  reason: string
  error: string | null
}

const emptyForm: MemoryFormState = {
  content: '',
  category: 'other',
  scope: 'global',
  priority: '100',
}

function memoryStatusLabel(status: string): string {
  if (status === 'all') return 'All'
  return getMemoryStatusDisplay(status).label
}

function suggestionStatusLabel(status: string): string {
  if (status === 'all') return 'All'
  if (status === 'archived') return 'Retired'
  return status.replace(/_/g, ' ')
}

function getErrorMessage(error: unknown) {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    const response = (error as {
      response?: { status?: number; data?: { detail?: unknown } }
    }).response
    if (response?.status === 409) {
      return 'An active duplicate memory already exists for this project.'
    }
    if (response?.status === 404) {
      return 'Memory not found for this project.'
    }
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
    if (Array.isArray(response?.data?.detail)) {
      return 'Memory validation failed. Check content, category, scope, and priority.'
    }
  }

  return 'Failed to update project memory.'
}

function getSuggestionErrorMessage(error: unknown) {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    const response = (error as {
      response?: { status?: number; data?: { detail?: unknown } }
    }).response
    if (response?.status === 409) {
      return 'An active memory already exists for this suggestion.'
    }
    if (response?.status === 422) {
      return 'Please provide a rejection reason.'
    }
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }

  return 'Failed to update memory suggestions.'
}

function getLifecycleErrorMessage(error: unknown, fallback: string) {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    const response = (error as {
      response?: { status?: number; data?: { detail?: unknown } }
    }).response
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
    if (Array.isArray(response?.data?.detail)) {
      return 'Memory lifecycle validation failed. Check the selected memories and reason.'
    }
    if (response?.status === 404) {
      return 'Memory or suggestion not found for this project.'
    }
    if (response?.status === 409) {
      return 'The backend rejected this lifecycle change. Refresh and check that the selected memories are still in use.'
    }
  }

  return fallback
}

function formatDate(value?: string | null) {
  if (!value) return 'Not set'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function formatActiveHistoryNote(fact: MemoryFact) {
  const earlierNote = fact.content.trim()
  const reason = fact.archived_reason?.trim()

  if (!earlierNote) {
    return reason
      ? `History note: Replaced an earlier memory. Reason: ${reason}`
      : 'History note: Replaced an earlier memory.'
  }

  return reason
    ? `History note: Replaced earlier note: "${earlierNote}" Reason: ${reason}`
    : `History note: Replaced earlier note: "${earlierNote}"`
}

function statusClass(status: string) {
  if (status === 'active') return 'border-green-200 bg-green-100 text-green-700'
  if (status === 'stale') return 'border-yellow-200 bg-yellow-100 text-yellow-800'
  if (status === 'archived') return 'border-slate-200 bg-slate-100 text-slate-700'
  if (status === 'historical') return 'border-blue-200 bg-blue-100 text-blue-700'
  return 'border-border bg-muted text-muted-foreground'
}

function toCreateRequest(form: MemoryFormState): MemoryCreateRequest {
  return {
    content: form.content.trim(),
    category: form.category,
    scope: form.scope,
    priority: Number(form.priority),
    source: 'manual',
  }
}

function toUpdateRequest(form: MemoryFormState): MemoryUpdateRequest {
  return {
    content: form.content.trim(),
    category: form.category,
    scope: form.scope,
    priority: Number(form.priority),
  }
}

function formFromFact(fact: MemoryFact): MemoryFormState {
  return {
    content: fact.content,
    category: CATEGORIES.includes(fact.category as MemoryCategory)
      ? fact.category as MemoryCategory
      : 'other',
    scope: SCOPES.includes(fact.scope as MemoryScope)
      ? fact.scope as MemoryScope
      : 'global',
    priority: String(fact.priority ?? 100),
  }
}

function SelectField({
  id,
  value,
  options,
  onChange,
  getLabel,
}: {
  id: string
  value: string
  options: string[]
  onChange: (event: ChangeEvent<HTMLSelectElement>) => void
  getLabel?: (option: string) => string
}) {
  return (
    <select
      id={id}
      value={value}
      onChange={onChange}
      className="h-8 w-full rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      {options.map(option => (
        <option key={option} value={option}>
          {getLabel ? getLabel(option) : option}
        </option>
      ))}
    </select>
  )
}

export default function ProjectMemoryPanel({ projectId }: ProjectMemoryPanelProps) {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<'all' | MemoryStatus>('all')
  const [categoryFilter, setCategoryFilter] = useState<'all' | MemoryCategory>('all')
  const [scopeFilter, setScopeFilter] = useState<'all' | MemoryScope>('all')
  const [suggestionStatusFilter, setSuggestionStatusFilter] =
    useState<'all' | MemorySuggestionStatus>('pending')
  const [form, setForm] = useState<MemoryFormState>(emptyForm)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editForm, setEditForm] = useState<MemoryFormState>(emptyForm)
  const [archiveReasons, setArchiveReasons] = useState<Record<string, string>>({})
  const [rejectionReasons, setRejectionReasons] = useState<Record<string, string>>({})
  const [editingSuggestionId, setEditingSuggestionId] = useState<string | null>(null)
  const [editedContents, setEditedContents] = useState<Record<string, string>>({})
  const [role, setRole] = useState<MemoryPreviewRole>('coder')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [suggestionMessage, setSuggestionMessage] = useState<string | null>(null)
  const [suggestionError, setSuggestionError] = useState<string | null>(null)
  const [markStaleDialog, setMarkStaleDialog] =
    useState<MarkStaleDialogState | null>(null)
  const [retireDialog, setRetireDialog] = useState<RetireDialogState | null>(null)
  const [supersedeDialog, setSupersedeDialog] =
    useState<SupersedeDialogState | null>(null)
  const [approveSupersedeDialog, setApproveSupersedeDialog] =
    useState<ApproveSupersedeDialogState | null>(null)

  const memoryQuery = useQuery({
    queryKey: ['project-memory', projectId],
    queryFn: () => memoryApi.listProjectMemory(projectId),
  })

  const suggestionsQuery = useQuery({
    queryKey: ['project-memory-suggestions', projectId],
    queryFn: () => memoryApi.listMemorySuggestions(projectId),
  })

  const previewQuery = useQuery({
    queryKey: ['project-memory-preview', projectId, role],
    queryFn: () => memoryApi.previewProjectMemory(projectId, role),
  })

  const sortedFacts = useMemo(() => {
    return [...(memoryQuery.data?.facts ?? [])]
      .filter(fact => {
        if (statusFilter !== 'all' && fact.status !== statusFilter) return false
        if (categoryFilter !== 'all' && fact.category !== categoryFilter) {
          return false
        }
        if (scopeFilter !== 'all' && fact.scope !== scopeFilter) return false
        return true
      })
      .sort((a, b) => {
        const statusDelta =
          (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99)
        if (statusDelta !== 0) return statusDelta
        return (a.priority ?? 100) - (b.priority ?? 100)
      })
  }, [categoryFilter, memoryQuery.data?.facts, scopeFilter, statusFilter])

  const activeFacts = useMemo(() => {
    return [...(memoryQuery.data?.facts ?? [])]
      .filter(fact => fact.status === 'active')
      .sort((a, b) => {
        return (a.priority ?? 100) - (b.priority ?? 100)
      })
  }, [memoryQuery.data?.facts])

  const factsById = useMemo(() => {
    return new Map(
      (memoryQuery.data?.facts ?? []).map(fact => [fact.id, fact]),
    )
  }, [memoryQuery.data?.facts])

  const historicalFactsByReplacementId = useMemo(() => {
    const lineage = new Map<string, MemoryFact[]>()

    for (const fact of memoryQuery.data?.facts ?? []) {
      if (fact.status !== 'historical' || !fact.superseded_by_fact_id) continue
      const existing = lineage.get(fact.superseded_by_fact_id) ?? []
      lineage.set(fact.superseded_by_fact_id, [...existing, fact])
    }

    return lineage
  }, [memoryQuery.data?.facts])

  const sortedSuggestions = useMemo(() => {
    return [...(suggestionsQuery.data?.suggestions ?? [])]
      .filter(suggestion => {
        return (
          suggestionStatusFilter === 'all' ||
          suggestion.status === suggestionStatusFilter
        )
      })
      .sort((a, b) => {
        const statusDelta =
          (SUGGESTION_STATUS_ORDER[a.status] ?? 99) -
          (SUGGESTION_STATUS_ORDER[b.status] ?? 99)
        if (statusDelta !== 0) return statusDelta
        return (a.priority ?? 100) - (b.priority ?? 100)
      })
  }, [suggestionStatusFilter, suggestionsQuery.data?.suggestions])

  const memoryTrustSummary = useMemo(
    () =>
      buildMemoryTrustSummary({
        facts: memoryQuery.data?.facts,
        suggestions: suggestionsQuery.data?.suggestions,
        factsLoading: memoryQuery.isLoading,
        suggestionsLoading: suggestionsQuery.isLoading,
        factsError: memoryQuery.isError,
        suggestionsError: suggestionsQuery.isError,
      }),
    [
      memoryQuery.data?.facts,
      memoryQuery.isError,
      memoryQuery.isLoading,
      suggestionsQuery.data?.suggestions,
      suggestionsQuery.isError,
      suggestionsQuery.isLoading,
    ],
  )

  function refreshMemory() {
    queryClient.invalidateQueries({ queryKey: ['project-memory', projectId] })
    queryClient.invalidateQueries({
      queryKey: ['project-memory-preview', projectId],
    })
  }

  function refreshSuggestions() {
    queryClient.invalidateQueries({
      queryKey: ['project-memory-suggestions', projectId],
    })
  }

  const createMutation = useMutation({
    mutationFn: (data: MemoryCreateRequest) =>
      memoryApi.createProjectMemory(projectId, data),
    onSuccess: () => {
      setForm(emptyForm)
      setMessage('Memory added.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setRetireDialog(previous =>
        previous
          ? {
              ...previous,
              error: getLifecycleErrorMessage(
                mutationError,
                'Failed to retire memory.',
              ),
            }
          : previous,
      )
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ memoryId, data }: { memoryId: string; data: MemoryUpdateRequest }) =>
      memoryApi.updateProjectMemory(projectId, memoryId, data),
    onSuccess: () => {
      setEditingId(null)
      setMessage('Memory saved.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(mutationError))
    },
  })

  const archiveMutation = useMutation({
    mutationFn: ({ memoryId, reason }: { memoryId: string; reason: string }) =>
      memoryApi.archiveProjectMemory(projectId, memoryId, reason),
    onSuccess: (_data, variables) => {
      setArchiveReasons(previous => ({ ...previous, [variables.memoryId]: '' }))
      setRetireDialog(null)
      setMessage('Memory retired.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(mutationError))
    },
  })

  const markStaleMutation = useMutation({
    mutationFn: ({ factId, reason }: { factId: string; reason: string }) =>
      memoryApi.markMemoryFactStale(projectId, factId, reason),
    onSuccess: () => {
      setMarkStaleDialog(null)
      setMessage('Memory marked as possibly outdated.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMarkStaleDialog(previous =>
        previous
          ? {
              ...previous,
              error: getLifecycleErrorMessage(
                mutationError,
                'Failed to mark memory as possibly outdated.',
              ),
            }
          : previous,
      )
    },
  })

  const supersedeMutation = useMutation({
    mutationFn: ({
      oldFactId,
      newFactId,
      reason,
    }: {
      oldFactId: string
      newFactId: string
      reason: string
    }) =>
      memoryApi.supersedeMemoryFact(projectId, oldFactId, newFactId, reason),
    onSuccess: () => {
      setSupersedeDialog(null)
      setMessage('Old memory replaced.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setSupersedeDialog(previous =>
        previous
          ? {
              ...previous,
              error: getLifecycleErrorMessage(
                mutationError,
                'Failed to replace old memory.',
              ),
            }
          : previous,
      )
    },
  })

  const verifyMutation = useMutation({
    mutationFn: (memoryId: string) =>
      memoryApi.verifyProjectMemory(projectId, memoryId),
    onSuccess: () => {
      setMessage('Memory verified.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(mutationError))
    },
  })

  const generateSuggestionsMutation = useMutation({
    mutationFn: () =>
      memoryApi.generateBootstrapMemorySuggestions(projectId, false),
    onSuccess: (data) => {
      setSuggestionMessage(
        data.suggestions.length > 0
          ? `${data.suggestions.length} bootstrap suggestion${data.suggestions.length === 1 ? '' : 's'} generated.`
          : 'No new bootstrap suggestions were generated.',
      )
      setSuggestionError(null)
      refreshSuggestions()
    },
    onError: (mutationError: unknown) => {
      setSuggestionMessage(null)
      setSuggestionError(getSuggestionErrorMessage(mutationError))
    },
  })

  const approveSuggestionMutation = useMutation({
    mutationFn: ({
      suggestionId,
      editedContent,
    }: {
      suggestionId: string
      editedContent?: string
    }) =>
      memoryApi.approveMemorySuggestion(projectId, suggestionId, editedContent),
    onSuccess: (_data, variables) => {
      setSuggestionMessage(
        variables.editedContent
          ? 'Suggestion edited and approved into active memory.'
          : 'Suggestion approved and added to active memory.',
      )
      setSuggestionError(null)
      setEditingSuggestionId(null)
      refreshSuggestions()
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setSuggestionMessage(null)
      setSuggestionError(getSuggestionErrorMessage(mutationError))
    },
  })

  const approveAndSupersedeMutation = useMutation({
    mutationFn: ({
      suggestionId,
      oldFactId,
      reason,
      editedContent,
    }: {
      suggestionId: string
      oldFactId: string
      reason: string
      editedContent?: string
    }) =>
      memoryApi.approveSuggestionAndSupersede(
        projectId,
        suggestionId,
        oldFactId,
        reason,
        editedContent,
      ),
    onSuccess: () => {
      setApproveSupersedeDialog(null)
      setSuggestionMessage('Suggestion approved and old memory replaced.')
      setSuggestionError(null)
      refreshSuggestions()
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setApproveSupersedeDialog(previous =>
        previous
          ? {
              ...previous,
              error: getLifecycleErrorMessage(
                mutationError,
                'Failed to approve suggestion and replace old memory.',
              ),
            }
          : previous,
      )
    },
  })

  const rejectSuggestionMutation = useMutation({
    mutationFn: ({ suggestionId, reason }: { suggestionId: string; reason: string }) =>
      memoryApi.rejectMemorySuggestion(projectId, suggestionId, reason),
    onSuccess: (_data, variables) => {
      setRejectionReasons(previous => ({
        ...previous,
        [variables.suggestionId]: '',
      }))
      setSuggestionMessage('Suggestion rejected.')
      setSuggestionError(null)
      refreshSuggestions()
    },
    onError: (mutationError: unknown) => {
      setSuggestionMessage(null)
      setSuggestionError(getSuggestionErrorMessage(mutationError))
    },
  })

  const updateForm = (field: keyof MemoryFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
      setForm(previous => ({ ...previous, [field]: event.target.value }))
    }

  const updateEditForm = (field: keyof MemoryFormState) =>
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
      setEditForm(previous => ({ ...previous, [field]: event.target.value }))
    }

  function validateForm(value: MemoryFormState) {
    const content = value.content.trim()
    const priority = Number(value.priority)
    if (content.length < 4) return 'Memory content must be at least 4 characters.'
    if (content.length > 400) return 'Memory content cannot exceed 400 characters.'
    if (!Number.isInteger(priority) || priority < 0 || priority > 1000) {
      return 'Priority must be an integer from 0 to 1000.'
    }
    return null
  }

  function handleCreate(event: FormEvent) {
    event.preventDefault()
    const validationError = validateForm(form)
    if (validationError) {
      setMessage(null)
      setError(validationError)
      return
    }
    createMutation.mutate(toCreateRequest(form))
  }

  function handleUpdate(memoryId: string) {
    const validationError = validateForm(editForm)
    if (validationError) {
      setMessage(null)
      setError(validationError)
      return
    }
    updateMutation.mutate({ memoryId, data: toUpdateRequest(editForm) })
  }

  function startEditing(fact: MemoryFact) {
    setEditingId(fact.id)
    setEditForm(formFromFact(fact))
    setMessage(null)
    setError(null)
  }

  function startMarkStale(fact: MemoryFact) {
    setMarkStaleDialog({ fact, reason: '', error: null })
    setMessage(null)
    setError(null)
  }

  function confirmMarkStale() {
    if (!markStaleDialog) return
    const reason = markStaleDialog.reason.trim()
    if (reason.length < 4) return
    markStaleMutation.mutate({ factId: markStaleDialog.fact.id, reason })
  }

  function startSupersede(fact: MemoryFact) {
    setSupersedeDialog({
      oldFact: fact,
      newFactId: '',
      reason: '',
      error: null,
    })
    setMessage(null)
    setError(null)
  }

  function confirmSupersede() {
    if (!supersedeDialog) return
    const reason = supersedeDialog.reason.trim()
    if (
      !supersedeDialog.newFactId ||
      supersedeDialog.newFactId === supersedeDialog.oldFact.id ||
      reason.length < 4
    ) {
      return
    }
    supersedeMutation.mutate({
      oldFactId: supersedeDialog.oldFact.id,
      newFactId: supersedeDialog.newFactId,
      reason,
    })
  }

  function startRetire(fact: MemoryFact) {
    const reason = (archiveReasons[fact.id] ?? '').trim()
    if (reason.length < 4) {
      setMessage(null)
      setError('Retire reason must be at least 4 characters.')
      return
    }
    setRetireDialog({ fact, reason, error: null })
    setMessage(null)
    setError(null)
  }

  function confirmRetire() {
    if (!retireDialog) return
    const reason = retireDialog.reason.trim()
    if (reason.length < 4) return
    archiveMutation.mutate({ memoryId: retireDialog.fact.id, reason })
  }

  function rejectSuggestion(suggestion: MemorySuggestion) {
    const reason = (rejectionReasons[suggestion.id] ?? '').trim()
    if (reason.length < 4) {
      setSuggestionMessage(null)
      setSuggestionError('Please provide a rejection reason.')
      return
    }
    rejectSuggestionMutation.mutate({ suggestionId: suggestion.id, reason })
  }

  function startEditingSuggestion(suggestion: MemorySuggestion) {
    setEditingSuggestionId(suggestion.id)
    setEditedContents(previous => ({
      ...previous,
      [suggestion.id]: previous[suggestion.id] ?? suggestion.content,
    }))
    setSuggestionMessage(null)
    setSuggestionError(null)
  }

  function startApproveAndSupersede(suggestion: MemorySuggestion) {
    setApproveSupersedeDialog({
      suggestion,
      oldFactId: '',
      editedContent: suggestion.content,
      reason: '',
      error: null,
    })
    setSuggestionMessage(null)
    setSuggestionError(null)
  }

  function confirmApproveAndSupersede() {
    if (!approveSupersedeDialog) return
    const reason = approveSupersedeDialog.reason.trim()
    const editedContent = approveSupersedeDialog.editedContent.trim()
    if (
      !approveSupersedeDialog.oldFactId ||
      reason.length < 4 ||
      editedContent.length < 4 ||
      editedContent.length > 400
    ) {
      return
    }
    approveAndSupersedeMutation.mutate({
      suggestionId: approveSupersedeDialog.suggestion.id,
      oldFactId: approveSupersedeDialog.oldFactId,
      reason,
      editedContent:
        editedContent === approveSupersedeDialog.suggestion.content.trim()
          ? undefined
          : editedContent,
    })
  }

  function approveEditedSuggestion(suggestion: MemorySuggestion) {
    const editedContent = (editedContents[suggestion.id] ?? '').trim()
    if (editedContent.length < 4 || editedContent.length > 400) {
      setSuggestionMessage(null)
      setSuggestionError('Edited memory content must be 4 to 400 characters.')
      return
    }
    approveSuggestionMutation.mutate({
      suggestionId: suggestion.id,
      editedContent,
    })
  }

  const activeStatusDisplay = getMemoryStatusDisplay('active')
  const staleStatusDisplay = getMemoryStatusDisplay('stale')
  const historicalStatusDisplay = getMemoryStatusDisplay('historical')
  const markStaleCanConfirm = Boolean(
    markStaleDialog && markStaleDialog.reason.trim().length >= 4,
  )
  const retireCanConfirm = Boolean(
    retireDialog && retireDialog.reason.trim().length >= 4,
  )
  const supersedeReplacementFacts = supersedeDialog
    ? activeFacts.filter(fact => fact.id !== supersedeDialog.oldFact.id)
    : []
  const selectedReplacementFact = supersedeDialog
    ? activeFacts.find(fact => fact.id === supersedeDialog.newFactId)
    : undefined
  const supersedeCanConfirm = Boolean(
    supersedeDialog &&
      selectedReplacementFact &&
      supersedeDialog.reason.trim().length >= 4,
  )
  const selectedApproveOldFact = approveSupersedeDialog
    ? activeFacts.find(fact => fact.id === approveSupersedeDialog.oldFactId)
    : undefined
  const approveSupersedeContent =
    approveSupersedeDialog?.editedContent.trim() ?? ''
  const approveSupersedeCanConfirm = Boolean(
    approveSupersedeDialog &&
      selectedApproveOldFact &&
      approveSupersedeDialog.reason.trim().length >= 4 &&
      approveSupersedeContent.length >= 4 &&
      approveSupersedeContent.length <= 400,
  )

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="text-base">Project Memory</CardTitle>
        <CardDescription>
          Memory is background context for the AI - not a command. Source code
          and your instructions always win.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-6">
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-900">
          Nothing is used until you approve it. If memory and the current code
          disagree, the code wins. Your explicit instructions always override
          memory.
        </div>
        <MemoryTrustStrip summary={memoryTrustSummary} />
        <MemoryAttentionPanel summary={memoryTrustSummary} />
        <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="grid gap-2">
              <Label htmlFor="memory-status-filter">Status</Label>
              <SelectField
                id="memory-status-filter"
                value={statusFilter}
                options={['all', ...STATUSES]}
                getLabel={memoryStatusLabel}
                onChange={event =>
                  setStatusFilter(event.target.value as 'all' | MemoryStatus)
                }
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="memory-category-filter">Category</Label>
              <SelectField
                id="memory-category-filter"
                value={categoryFilter}
                options={['all', ...CATEGORIES]}
                onChange={event =>
                  setCategoryFilter(event.target.value as 'all' | MemoryCategory)
                }
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="memory-scope-filter">Scope</Label>
              <SelectField
                id="memory-scope-filter"
                value={scopeFilter}
                options={['all', ...SCOPES]}
                onChange={event =>
                  setScopeFilter(event.target.value as 'all' | MemoryScope)
                }
              />
            </div>
          </div>
        </div>

        <form onSubmit={handleCreate} className="grid gap-3 rounded-lg border p-4">
          <div>
            <h3 className="text-sm font-semibold">Add Memory Note</h3>
            <p className="text-xs text-muted-foreground">
              Add only human-approved project memory. No secrets, credentials,
              emails, phone numbers, or prompt instructions.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="memory-content">Content</Label>
            <Textarea
              id="memory-content"
              value={form.content}
              maxLength={400}
              onChange={updateForm('content')}
              rows={3}
              placeholder="Backend uses Python 3.11 and FastAPI."
            />
            <p className="text-xs text-muted-foreground">
              {form.content.trim().length}/400 characters
            </p>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="grid gap-2">
              <Label htmlFor="memory-category">Category</Label>
              <SelectField
                id="memory-category"
                value={form.category}
                options={CATEGORIES}
                onChange={updateForm('category')}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="memory-scope">Scope</Label>
              <SelectField
                id="memory-scope"
                value={form.scope}
                options={SCOPES}
                onChange={updateForm('scope')}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="memory-priority">Priority</Label>
              <Input
                id="memory-priority"
                type="number"
                min={0}
                max={1000}
                value={form.priority}
                onChange={updateForm('priority')}
              />
            </div>
          </div>
          <Button
            type="submit"
            disabled={createMutation.isPending}
            className="w-fit"
          >
            {createMutation.isPending ? 'Adding...' : 'Add Memory Note'}
          </Button>
        </form>

        {(message || error) && (
          <div
            className={
              error
                ? 'rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700'
                : 'rounded border border-green-200 bg-green-50 px-3 py-2 text-sm font-medium text-green-700'
            }
          >
            {error || message}
          </div>
        )}

        <div className="grid gap-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold">Memory Notes</h3>
              <p className="text-xs text-muted-foreground">
                In-use memories appear first when the status filter is all.
                Retire similar notes or mark outdated notes manually.
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refreshMemory()}
              disabled={memoryQuery.isFetching || previewQuery.isFetching}
            >
              Refresh
            </Button>
          </div>
          {memoryQuery.isLoading && (
            <p className="text-sm text-muted-foreground">Loading memory...</p>
          )}
          {!memoryQuery.isLoading && sortedFacts.length === 0 && (
            <div className="rounded-lg border border-dashed p-4">
              <p className="text-sm font-medium">No memory notes yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                No approved memory yet. Generate bootstrap suggestions or add
                memory manually.
              </p>
            </div>
          )}
          {sortedFacts.map(fact => {
            const isEditing = editingId === fact.id
            const isActive = fact.status === 'active'
            const isArchived = fact.status === 'archived'
            const statusDisplay = getMemoryStatusDisplay(fact.status)
            const replacementFact = fact.superseded_by_fact_id
              ? factsById.get(fact.superseded_by_fact_id)
              : undefined
            const supersededFacts = fact.status === 'active'
              ? historicalFactsByReplacementId.get(fact.id) ?? []
              : []

            return (
              <div key={fact.id} className="rounded-lg border p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant="outline"
                      className={statusDisplay.className}
                      title={statusDisplay.tooltip}
                    >
                      {statusDisplay.label}
                    </Badge>
                    <Badge variant="outline">{fact.category}</Badge>
                    <Badge variant="outline">{fact.scope}</Badge>
                    <span className="text-xs text-muted-foreground">
                      priority {fact.priority}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => verifyMutation.mutate(fact.id)}
                      disabled={verifyMutation.isPending}
                    >
                      Verify
                    </Button>
                    {isActive && (
                      <>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => startMarkStale(fact)}
                        >
                          Mark as possibly outdated
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => startSupersede(fact)}
                        >
                          Replace this memory
                        </Button>
                      </>
                    )}
                    {!isArchived && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => startEditing(fact)}
                      >
                        Edit
                      </Button>
                    )}
                  </div>
                </div>

                {isEditing ? (
                  <div className="mt-3 grid gap-3">
                    <Textarea
                      value={editForm.content}
                      maxLength={400}
                      onChange={updateEditForm('content')}
                      rows={3}
                    />
                    <div className="grid gap-3 sm:grid-cols-3">
                      <SelectField
                        id={`edit-category-${fact.id}`}
                        value={editForm.category}
                        options={CATEGORIES}
                        onChange={updateEditForm('category')}
                      />
                      <SelectField
                        id={`edit-scope-${fact.id}`}
                        value={editForm.scope}
                        options={SCOPES}
                        onChange={updateEditForm('scope')}
                      />
                      <Input
                        type="number"
                        min={0}
                        max={1000}
                        value={editForm.priority}
                        onChange={updateEditForm('priority')}
                      />
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        onClick={() => handleUpdate(fact.id)}
                        disabled={updateMutation.isPending}
                      >
                        {updateMutation.isPending ? 'Saving...' : 'Save'}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setEditingId(null)}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                ) : (
                  <p className="mt-3 text-sm leading-6">{fact.content}</p>
                )}

                {fact.status === 'historical' && fact.superseded_by_fact_id && (
                  <p className="mt-3 rounded-lg bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                    {replacementFact
                      ? `Replaced by your approval -> ${replacementFact.content}`
                      : 'Replaced by another approved memory.'}
                  </p>
                )}

                {fact.status === 'active' && supersededFacts.length > 0 && (
                  <div className="mt-3 grid gap-1 rounded-lg bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                    {supersededFacts.slice(0, 3).map(historicalFact => (
                      <p key={historicalFact.id}>
                        {formatActiveHistoryNote(historicalFact)}
                      </p>
                    ))}
                    {supersededFacts.length > 3 && (
                      <p>
                        Replaced {supersededFacts.length - 3} more old
                        memories.
                      </p>
                    )}
                  </div>
                )}

                <div className="mt-3 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                  <p>Created: {formatDate(fact.created_at)}</p>
                  <p>Updated: {formatDate(fact.updated_at)}</p>
                  <p>
                    Last checked:{' '}
                    {fact.last_verified_at
                      ? formatDate(fact.last_verified_at)
                      : 'Not checked yet'}
                  </p>
                  {fact.archived_reason &&
                    !(fact.status === 'active' && supersededFacts.length > 0) && (
                      <p className="sm:col-span-2">
                        Lifecycle reason: {fact.archived_reason}
                      </p>
                    )}
                </div>

                {!isArchived && (
                  <div className="mt-3 grid gap-2 rounded-lg bg-muted/30 p-3">
                    <Label htmlFor={`archive-reason-${fact.id}`}>
                      Retire Reason
                    </Label>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Input
                        id={`archive-reason-${fact.id}`}
                        value={archiveReasons[fact.id] ?? ''}
                        onChange={event =>
                          setArchiveReasons(previous => ({
                            ...previous,
                            [fact.id]: event.target.value,
                          }))
                        }
                        placeholder="Outdated after backend migration."
                      />
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => startRetire(fact)}
                        disabled={archiveMutation.isPending}
                      >
                        Retire memory
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
            <div>
              <h3 className="text-sm font-semibold">Suggested memories</h3>
              <p className="text-xs text-muted-foreground">
                Suggested from repository/config files. Nothing is used until
                you approve it.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Source code and explicit user instructions still win over
                approved memory.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Approving inaccurate memory can mislead future runs. Approve
                only what is true today. Rejected suggestions are harmless -
                they are never used and can be ignored.
              </p>
            </div>
            <Button
              onClick={() => generateSuggestionsMutation.mutate()}
              disabled={generateSuggestionsMutation.isPending}
              className="w-fit"
            >
              {generateSuggestionsMutation.isPending
                ? 'Generating...'
                : 'Generate Suggestions'}
            </Button>
          </div>

          <div className="grid gap-2 sm:w-56">
            <Label htmlFor="memory-suggestion-status-filter">
              Suggestion Status
            </Label>
            <SelectField
              id="memory-suggestion-status-filter"
              value={suggestionStatusFilter}
              options={['all', ...SUGGESTION_STATUSES]}
              getLabel={suggestionStatusLabel}
              onChange={event =>
                setSuggestionStatusFilter(
                  event.target.value as 'all' | MemorySuggestionStatus,
                )
              }
            />
          </div>

          {(suggestionMessage || suggestionError) && (
            <div
              className={
                suggestionError
                  ? 'rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700'
                  : 'rounded border border-green-200 bg-green-50 px-3 py-2 text-sm font-medium text-green-700'
              }
            >
              {suggestionError || suggestionMessage}
            </div>
          )}

          {suggestionsQuery.isLoading && (
            <p className="text-sm text-muted-foreground">
              Loading suggestions...
            </p>
          )}
          {!suggestionsQuery.isLoading && sortedSuggestions.length === 0 && (
            <div className="rounded-lg border border-dashed p-4">
              <p className="text-sm font-medium">
                {suggestionStatusFilter === 'pending'
                  ? 'No pending suggestions.'
                  : 'No suggestions match this filter.'}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                Generate suggested memories to review repo-derived memory
                proposals.
              </p>
            </div>
          )}

          {sortedSuggestions.map(suggestion => {
            const isPending = suggestion.status === 'pending'
            return (
              <div key={suggestion.id} className="rounded-lg border p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant="outline"
                      className={statusClass(suggestion.status)}
                    >
                      {suggestionStatusLabel(suggestion.status)}
                    </Badge>
                    <Badge variant="outline">{suggestion.category}</Badge>
                    <Badge variant="outline">{suggestion.scope}</Badge>
                    {suggestion.risk_level && (
                      <Badge variant="outline">
                        risk {suggestion.risk_level}
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">
                      priority {suggestion.priority}
                    </span>
                    {suggestion.source && (
                      <span className="text-xs text-muted-foreground">
                        source {suggestion.source}
                      </span>
                    )}
                  </div>
                  {isPending && (
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        onClick={() =>
                          approveSuggestionMutation.mutate({
                            suggestionId: suggestion.id,
                          })
                        }
                        disabled={approveSuggestionMutation.isPending}
                      >
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          editingSuggestionId === suggestion.id
                            ? setEditingSuggestionId(null)
                            : startEditingSuggestion(suggestion)
                        }
                      >
                        {editingSuggestionId === suggestion.id
                          ? 'Cancel edit'
                          : 'Edit & approve'}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => startApproveAndSupersede(suggestion)}
                      >
                        Approve & replace an old memory...
                      </Button>
                    </div>
                  )}
                </div>

                <p className="mt-3 text-sm leading-6">{suggestion.content}</p>

                {(suggestion.source_type ||
                  suggestion.source_run_id ||
                  suggestion.rationale) && (
                  <div className="mt-3 rounded-lg bg-muted/30 p-3 text-xs text-muted-foreground">
                    {(suggestion.source_type || suggestion.source_run_id) && (
                      <p>
                        {suggestion.source_type && (
                          <span>from {suggestion.source_type}</span>
                        )}
                        {suggestion.source_chunk_number != null && (
                          <span> · chunk {suggestion.source_chunk_number}</span>
                        )}
                        {suggestion.source_run_id && (
                          <span>
                            {' · run '}
                            <span className="font-mono">
                              {suggestion.source_run_id.slice(0, 8)}
                            </span>
                          </span>
                        )}
                      </p>
                    )}
                    {suggestion.rationale && (
                      <p className="mt-1">{suggestion.rationale}</p>
                    )}
                  </div>
                )}

                {isPending && editingSuggestionId === suggestion.id && (
                  <div className="mt-3 grid gap-2 rounded-lg bg-muted/30 p-3">
                    <Label htmlFor={`edit-suggestion-${suggestion.id}`}>
                      Edited Memory Content
                    </Label>
                    <Textarea
                      id={`edit-suggestion-${suggestion.id}`}
                      value={editedContents[suggestion.id] ?? ''}
                      maxLength={400}
                      rows={3}
                      onChange={event =>
                        setEditedContents(previous => ({
                          ...previous,
                          [suggestion.id]: event.target.value,
                        }))
                      }
                    />
                    <div className="flex justify-end">
                      <Button
                        size="sm"
                        onClick={() => approveEditedSuggestion(suggestion)}
                        disabled={approveSuggestionMutation.isPending}
                      >
                        Approve edited
                      </Button>
                    </div>
                  </div>
                )}

                {(suggestion.evidence_path || suggestion.evidence_excerpt) && (
                  <div className="mt-3 rounded-lg bg-muted/30 p-3 text-xs text-muted-foreground">
                    {suggestion.evidence_path && (
                      <p className="font-mono">
                        Found in: {suggestion.evidence_path}
                      </p>
                    )}
                    {suggestion.evidence_excerpt && (
                      <p className="mt-1">{suggestion.evidence_excerpt}</p>
                    )}
                  </div>
                )}

                {isPending && (
                  <div className="mt-3 grid gap-2 rounded-lg bg-muted/30 p-3">
                    <Label htmlFor={`reject-suggestion-${suggestion.id}`}>
                      Rejection Reason
                    </Label>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Input
                        id={`reject-suggestion-${suggestion.id}`}
                        value={rejectionReasons[suggestion.id] ?? ''}
                        onChange={event =>
                          setRejectionReasons(previous => ({
                            ...previous,
                            [suggestion.id]: event.target.value,
                          }))
                        }
                        placeholder="Not accurate for this project."
                      />
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => rejectSuggestion(suggestion)}
                        disabled={rejectSuggestionMutation.isPending}
                      >
                        Reject
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
            <div>
              <h3 className="text-sm font-semibold">
                Preview what the AI sees
              </h3>
              <p className="text-xs text-muted-foreground">
                Shows the advisory memory block returned by the backend.
              </p>
            </div>
            <div className="w-full sm:w-44">
              <SelectField
                id="memory-preview-role"
                value={role}
                options={ROLES}
                onChange={event => setRole(event.target.value as MemoryPreviewRole)}
              />
            </div>
          </div>
          {previewQuery.isLoading && (
            <p className="text-sm text-muted-foreground">Loading preview...</p>
          )}
          {!previewQuery.isLoading && previewQuery.data?.empty && (
            <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
              No approved memory would be shown to the AI for this role.
            </p>
          )}
          {!previewQuery.isLoading && previewQuery.data && !previewQuery.data.empty && (
            <pre className="max-h-80 overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-5 text-slate-100">
              {previewQuery.data.memory_block}
            </pre>
          )}
        </div>

        <Dialog
          open={Boolean(markStaleDialog)}
          onOpenChange={open => {
            if (!open && !markStaleMutation.isPending) setMarkStaleDialog(null)
          }}
        >
          <DialogContent className="sm:max-w-xl">
            {markStaleDialog && (
              <>
                <DialogHeader>
                  <DialogTitle>Mark as possibly outdated</DialogTitle>
                  <DialogDescription>
                    This memory stops being shown to the AI. It is kept for
                    review, and Pipewright does not check the code
                    automatically.
                  </DialogDescription>
                </DialogHeader>

                <div className="grid gap-3">
                  <div className="grid gap-1">
                    <p className="text-xs font-medium text-muted-foreground">
                      Memory
                    </p>
                    <p className="whitespace-pre-wrap rounded border bg-muted/30 p-3 text-sm leading-6">
                      {markStaleDialog.fact.content}
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span>Current status:</span>
                    <Badge
                      variant="outline"
                      className={activeStatusDisplay.className}
                    >
                      {activeStatusDisplay.label}
                    </Badge>
                    <span>Resulting status:</span>
                    <Badge
                      variant="outline"
                      className={staleStatusDisplay.className}
                    >
                      {staleStatusDisplay.label}
                    </Badge>
                  </div>

                  <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    Future runs will not be shown this memory. If memory and
                    the current code disagree, the code wins.
                  </p>

                  <div className="grid gap-2">
                    <Label htmlFor="mark-stale-reason">Reason</Label>
                    <Input
                      id="mark-stale-reason"
                      value={markStaleDialog.reason}
                      onChange={event =>
                        setMarkStaleDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                reason: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      placeholder="Outdated after framework migration."
                    />
                    <p className="text-xs text-muted-foreground">
                      At least 4 characters. Stored as the lifecycle reason.
                    </p>
                  </div>

                  {markStaleDialog.error && (
                    <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                      {markStaleDialog.error}
                    </p>
                  )}
                </div>

                <DialogFooter>
                  <Button
                    variant="outline"
                    onClick={() => setMarkStaleDialog(null)}
                    disabled={markStaleMutation.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={confirmMarkStale}
                    disabled={!markStaleCanConfirm || markStaleMutation.isPending}
                  >
                    {markStaleMutation.isPending
                      ? 'Marking...'
                      : 'Mark as possibly outdated'}
                  </Button>
                </DialogFooter>
              </>
            )}
          </DialogContent>
        </Dialog>

        <Dialog
          open={Boolean(retireDialog)}
          onOpenChange={open => {
            if (!open && !archiveMutation.isPending) setRetireDialog(null)
          }}
        >
          <DialogContent className="sm:max-w-xl">
            {retireDialog && (
              <>
                <DialogHeader>
                  <DialogTitle>Retire this memory</DialogTitle>
                  <DialogDescription>
                    This memory stops being shown to the AI and is kept for the
                    record. Future runs will not be shown it.
                  </DialogDescription>
                </DialogHeader>

                <div className="grid gap-3">
                  <div className="grid gap-1">
                    <p className="text-xs font-medium text-muted-foreground">
                      Memory
                    </p>
                    <p className="whitespace-pre-wrap rounded border bg-muted/30 p-3 text-sm leading-6">
                      {retireDialog.fact.content}
                    </p>
                  </div>

                  <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    Retiring does not remove this memory. It is kept for the
                    record and can be reviewed later.
                  </p>

                  <div className="grid gap-2">
                    <Label htmlFor="retire-reason">Reason</Label>
                    <Input
                      id="retire-reason"
                      value={retireDialog.reason}
                      onChange={event =>
                        setRetireDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                reason: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      placeholder="No longer true after migration."
                    />
                    <p className="text-xs text-muted-foreground">
                      At least 4 characters. Stored as the lifecycle reason.
                    </p>
                  </div>

                  {retireDialog.error && (
                    <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                      {retireDialog.error}
                    </p>
                  )}
                </div>

                <DialogFooter>
                  <Button
                    variant="outline"
                    onClick={() => setRetireDialog(null)}
                    disabled={archiveMutation.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={confirmRetire}
                    disabled={!retireCanConfirm || archiveMutation.isPending}
                  >
                    {archiveMutation.isPending ? 'Retiring...' : 'Retire memory'}
                  </Button>
                </DialogFooter>
              </>
            )}
          </DialogContent>
        </Dialog>

        <Dialog
          open={Boolean(supersedeDialog)}
          onOpenChange={open => {
            if (!open && !supersedeMutation.isPending) setSupersedeDialog(null)
          }}
        >
          <DialogContent className="sm:max-w-2xl">
            {supersedeDialog && (
              <>
                <DialogHeader>
                  <DialogTitle>Replace an older memory</DialogTitle>
                  <DialogDescription>
                    Choose the approved memory that replaces this older memory.
                    Newer does not mean true. You decide which memory to use.
                  </DialogDescription>
                </DialogHeader>

                <div className="grid gap-3">
                  <div className="grid gap-1">
                    <p className="text-xs font-medium text-muted-foreground">
                      Older memory
                    </p>
                    <p className="whitespace-pre-wrap rounded border bg-muted/30 p-3 text-sm leading-6">
                      {supersedeDialog.oldFact.content}
                    </p>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="supersede-replacement">
                      Replacement memory
                    </Label>
                    <select
                      id="supersede-replacement"
                      value={supersedeDialog.newFactId}
                      onChange={event =>
                        setSupersedeDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                newFactId: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      className="h-8 w-full rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                      disabled={
                        memoryQuery.isLoading ||
                        supersedeReplacementFacts.length === 0
                      }
                    >
                      <option value="">
                        {memoryQuery.isLoading
                          ? 'Loading memories...'
                          : 'Select replacement memory'}
                      </option>
                      {supersedeReplacementFacts.map(fact => (
                        <option key={fact.id} value={fact.id}>
                          {fact.content}
                        </option>
                      ))}
                    </select>
                    {supersedeReplacementFacts.length === 0 &&
                      !memoryQuery.isLoading && (
                        <p className="text-xs text-muted-foreground">
                          No other in-use memories are available as
                          replacements.
                        </p>
                      )}
                  </div>

                  {selectedReplacementFact && (
                    <div className="grid gap-1">
                      <p className="text-xs font-medium text-muted-foreground">
                        Replacement memory
                      </p>
                      <p className="whitespace-pre-wrap rounded border bg-muted/30 p-3 text-sm leading-6">
                        {selectedReplacementFact.content}
                      </p>
                    </div>
                  )}

                  <div className="grid gap-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800">
                    <p>
                      The older memory moves to Replaced and stops being used.
                      Future runs use the replacement memory, not the old one.
                    </p>
                    <p>
                      Replacing does not remove the old memory - it is kept in
                      history.
                    </p>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge
                        variant="outline"
                        className={activeStatusDisplay.className}
                      >
                        {activeStatusDisplay.label}
                      </Badge>
                      <span>to</span>
                      <Badge
                        variant="outline"
                        className={historicalStatusDisplay.className}
                      >
                        {historicalStatusDisplay.label}
                      </Badge>
                    </div>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="supersede-reason">Reason</Label>
                    <Input
                      id="supersede-reason"
                      value={supersedeDialog.reason}
                      onChange={event =>
                        setSupersedeDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                reason: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      placeholder="Replaced after confirmed framework migration."
                    />
                    <p className="text-xs text-muted-foreground">
                      At least 4 characters. The selected direction is explicit.
                    </p>
                  </div>

                  {supersedeDialog.error && (
                    <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                      {supersedeDialog.error}
                    </p>
                  )}
                </div>

                <DialogFooter>
                  <Button
                    variant="outline"
                    onClick={() => setSupersedeDialog(null)}
                    disabled={supersedeMutation.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={confirmSupersede}
                    disabled={!supersedeCanConfirm || supersedeMutation.isPending}
                  >
                    {supersedeMutation.isPending
                      ? 'Replacing...'
                      : 'Replace old memory'}
                  </Button>
                </DialogFooter>
              </>
            )}
          </DialogContent>
        </Dialog>

        <Dialog
          open={Boolean(approveSupersedeDialog)}
          onOpenChange={open => {
            if (!open && !approveAndSupersedeMutation.isPending) {
              setApproveSupersedeDialog(null)
            }
          }}
        >
          <DialogContent className="sm:max-w-2xl">
            {approveSupersedeDialog && (
              <>
                <DialogHeader>
                  <DialogTitle>Approve a suggestion that needs review</DialogTitle>
                  <DialogDescription>
                    This becomes active memory the AI may be shown. Nothing is
                    removed automatically, and inaccurate memory can mislead
                    future runs.
                  </DialogDescription>
                </DialogHeader>

                <div className="grid gap-3">
                  <div className="grid gap-2">
                    <Label htmlFor="approve-supersede-content">
                      Suggestion content
                    </Label>
                    <Textarea
                      id="approve-supersede-content"
                      value={approveSupersedeDialog.editedContent}
                      maxLength={400}
                      rows={3}
                      onChange={event =>
                        setApproveSupersedeDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                editedContent: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                    />
                    <p className="text-xs text-muted-foreground">
                      {approveSupersedeContent.length}/400 characters. Edits
                      are validated by the backend before approval.
                    </p>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="approve-supersede-old-fact">
                      Old memory to replace
                    </Label>
                    <select
                      id="approve-supersede-old-fact"
                      value={approveSupersedeDialog.oldFactId}
                      onChange={event =>
                        setApproveSupersedeDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                oldFactId: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      className="h-8 w-full rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                      disabled={memoryQuery.isLoading || activeFacts.length === 0}
                    >
                      <option value="">
                        {memoryQuery.isLoading
                          ? 'Loading memories...'
                          : 'Select old memory'}
                      </option>
                      {activeFacts.map(fact => (
                        <option key={fact.id} value={fact.id}>
                          {fact.content}
                        </option>
                      ))}
                    </select>
                    {activeFacts.length === 0 && !memoryQuery.isLoading && (
                      <p className="text-xs text-muted-foreground">
                        No in-use memories are available to replace.
                      </p>
                    )}
                  </div>

                  {selectedApproveOldFact && (
                    <div className="grid gap-1">
                      <p className="text-xs font-medium text-muted-foreground">
                        Selected old memory
                      </p>
                      <p className="whitespace-pre-wrap rounded border bg-muted/30 p-3 text-sm leading-6">
                        {selectedApproveOldFact.content}
                      </p>
                    </div>
                  )}

                  <div className="grid gap-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800">
                    <p>
                      This suggestion will become active memory the AI may be
                      shown. The selected old memory will become Replaced and
                      stop being used.
                    </p>
                    <p>
                      Replacing does not remove the old memory - it is kept in
                      history. Code and explicit instructions still win.
                    </p>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="approve-supersede-reason">Reason</Label>
                    <Input
                      id="approve-supersede-reason"
                      value={approveSupersedeDialog.reason}
                      onChange={event =>
                        setApproveSupersedeDialog(previous =>
                          previous
                            ? {
                                ...previous,
                                reason: event.target.value,
                                error: null,
                              }
                            : previous,
                        )
                      }
                      placeholder="Suggestion replaces outdated approved memory."
                    />
                    <p className="text-xs text-muted-foreground">
                      At least 4 characters. The selected old memory becomes
                      Replaced only after backend approval succeeds.
                    </p>
                  </div>

                  {approveSupersedeDialog.error && (
                    <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
                      {approveSupersedeDialog.error}
                    </p>
                  )}
                </div>

                <DialogFooter>
                  <Button
                    variant="outline"
                    onClick={() => setApproveSupersedeDialog(null)}
                    disabled={approveAndSupersedeMutation.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={confirmApproveAndSupersede}
                    disabled={
                      !approveSupersedeCanConfirm ||
                      approveAndSupersedeMutation.isPending
                    }
                  >
                    {approveAndSupersedeMutation.isPending
                      ? 'Approving...'
                      : 'Approve & replace an old memory'}
                  </Button>
                </DialogFooter>
              </>
            )}
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
