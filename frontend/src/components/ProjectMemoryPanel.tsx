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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { getMemoryStatusDisplay } from '@/utils/memoryStatusDisplay'

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

const emptyForm: MemoryFormState = {
  content: '',
  category: 'other',
  scope: 'global',
  priority: '100',
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
      return 'An active duplicate memory fact already exists for this project.'
    }
    if (response?.status === 404) {
      return 'Memory fact not found for this project.'
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
      return 'An active memory fact already exists for this suggestion.'
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

function formatDate(value?: string | null) {
  if (!value) return 'Not set'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
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
}: {
  id: string
  value: string
  options: string[]
  onChange: (event: ChangeEvent<HTMLSelectElement>) => void
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
          {option}
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

  const filters = useMemo(() => {
    return {
      ...(statusFilter === 'all' ? {} : { status: statusFilter }),
      ...(categoryFilter === 'all' ? {} : { category: categoryFilter }),
      ...(scopeFilter === 'all' ? {} : { scope: scopeFilter }),
    }
  }, [categoryFilter, scopeFilter, statusFilter])

  const memoryQuery = useQuery({
    queryKey: [
      'project-memory',
      projectId,
      statusFilter,
      categoryFilter,
      scopeFilter,
    ],
    queryFn: () => memoryApi.listProjectMemory(projectId, filters),
  })

  const suggestionFilters = useMemo(() => {
    return {
      ...(suggestionStatusFilter === 'all'
        ? {}
        : { status: suggestionStatusFilter }),
    }
  }, [suggestionStatusFilter])

  const suggestionsQuery = useQuery({
    queryKey: ['project-memory-suggestions', projectId, suggestionStatusFilter],
    queryFn: () => memoryApi.listMemorySuggestions(projectId, suggestionFilters),
  })

  const previewQuery = useQuery({
    queryKey: ['project-memory-preview', projectId, role],
    queryFn: () => memoryApi.previewProjectMemory(projectId, role),
  })

  const sortedFacts = useMemo(() => {
    return [...(memoryQuery.data?.facts ?? [])].sort((a, b) => {
      const statusDelta =
        (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99)
      if (statusDelta !== 0) return statusDelta
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
    return [...(suggestionsQuery.data?.suggestions ?? [])].sort((a, b) => {
      const statusDelta =
        (SUGGESTION_STATUS_ORDER[a.status] ?? 99) -
        (SUGGESTION_STATUS_ORDER[b.status] ?? 99)
      if (statusDelta !== 0) return statusDelta
      return (a.priority ?? 100) - (b.priority ?? 100)
    })
  }, [suggestionsQuery.data?.suggestions])

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
      setMessage('Memory fact added.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(mutationError))
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ memoryId, data }: { memoryId: string; data: MemoryUpdateRequest }) =>
      memoryApi.updateProjectMemory(projectId, memoryId, data),
    onSuccess: () => {
      setEditingId(null)
      setMessage('Memory fact saved.')
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
      setMessage('Memory fact archived.')
      setError(null)
      refreshMemory()
    },
    onError: (mutationError: unknown) => {
      setMessage(null)
      setError(getErrorMessage(mutationError))
    },
  })

  const verifyMutation = useMutation({
    mutationFn: (memoryId: string) =>
      memoryApi.verifyProjectMemory(projectId, memoryId),
    onSuccess: () => {
      setMessage('Memory fact verified.')
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

  function archiveFact(fact: MemoryFact) {
    const reason = (archiveReasons[fact.id] ?? '').trim()
    if (reason.length < 4) {
      setMessage(null)
      setError('Archive reason must be at least 4 characters.')
      return
    }
    archiveMutation.mutate({ memoryId: fact.id, reason })
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

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="text-base">Project Memory</CardTitle>
        <CardDescription>
          Memory facts are advisory context injected into AI prompts. Source
          code and explicit user instructions always win on conflict.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-6">
        <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="grid gap-2">
              <Label htmlFor="memory-status-filter">Status</Label>
              <SelectField
                id="memory-status-filter"
                value={statusFilter}
                options={['all', ...STATUSES]}
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
            <h3 className="text-sm font-semibold">Add Memory Fact</h3>
            <p className="text-xs text-muted-foreground">
              Add only human-approved project facts. No secrets, credentials,
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
            {createMutation.isPending ? 'Adding...' : 'Add Memory Fact'}
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
              <h3 className="text-sm font-semibold">Memory Facts</h3>
              <p className="text-xs text-muted-foreground">
                Active facts appear first when the status filter is all.
                Archive similar or outdated facts manually.
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
              <p className="text-sm font-medium">No memory facts yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                No approved memory yet. Generate bootstrap suggestions or add
                memory manually.
              </p>
            </div>
          )}
          {sortedFacts.map(fact => {
            const isEditing = editingId === fact.id
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
                      : 'Replaced by another approved fact.'}
                  </p>
                )}

                {fact.status === 'active' && supersededFacts.length > 0 && (
                  <div className="mt-3 grid gap-1 rounded-lg bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                    {supersededFacts.slice(0, 3).map(historicalFact => (
                      <p key={historicalFact.id}>
                        Supersedes &lt;- {historicalFact.content}
                      </p>
                    ))}
                    {supersededFacts.length > 3 && (
                      <p>
                        Supersedes {supersededFacts.length - 3} more historical
                        facts.
                      </p>
                    )}
                  </div>
                )}

                <div className="mt-3 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                  <p>Created: {formatDate(fact.created_at)}</p>
                  <p>Updated: {formatDate(fact.updated_at)}</p>
                  <p>Verified: {formatDate(fact.last_verified_at)}</p>
                  {fact.archived_reason && (
                    <p className="sm:col-span-2">
                      Archived reason: {fact.archived_reason}
                    </p>
                  )}
                </div>

                {!isArchived && (
                  <div className="mt-3 grid gap-2 rounded-lg bg-muted/30 p-3">
                    <Label htmlFor={`archive-reason-${fact.id}`}>
                      Archive Reason
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
                        onClick={() => archiveFact(fact)}
                        disabled={archiveMutation.isPending}
                      >
                        Archive
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
              <h3 className="text-sm font-semibold">Bootstrap Suggestions</h3>
              <p className="text-xs text-muted-foreground">
                Generate suggested memory facts from repository/config files.
                Suggestions are not injected until you approve them.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Source code and explicit user instructions still win over
                approved memory.
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
                Generate bootstrap suggestions to review repo-derived memory
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
                      {suggestion.status}
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
                        Evidence: {suggestion.evidence_path}
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
              <h3 className="text-sm font-semibold">Prompt Preview</h3>
              <p className="text-xs text-muted-foreground">
                Shows the exact advisory memory block returned by the backend.
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
              No active memory would be injected for this role.
            </p>
          )}
          {!previewQuery.isLoading && previewQuery.data && !previewQuery.data.empty && (
            <pre className="max-h-80 overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-5 text-slate-100">
              {previewQuery.data.memory_block}
            </pre>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
