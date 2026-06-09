import type { ChangeEvent } from 'react'

import type { MemorySuggestion } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

interface PendingMemorySuggestionCardProps {
  suggestion: MemorySuggestion
  isEditing: boolean
  editedContent: string
  rejectionReason: string
  isApprovePending: boolean
  isRejectPending: boolean
  onApprove: (suggestion: MemorySuggestion) => void
  onToggleEdit: (suggestion: MemorySuggestion) => void
  onApproveEdited: (suggestion: MemorySuggestion) => void
  onEditedContentChange: (suggestionId: string, value: string) => void
  onApproveAndReplace: (suggestion: MemorySuggestion) => void
  onReject: (suggestion: MemorySuggestion) => void
  onRejectionReasonChange: (suggestionId: string, value: string) => void
}

function shortRunId(value?: string | null) {
  return value ? value.slice(0, 8) : null
}

function sourceSummary(suggestion: MemorySuggestion): string | null {
  const parts = [
    suggestion.source_type,
    suggestion.source_chunk_number != null
      ? `chunk ${suggestion.source_chunk_number}`
      : null,
    suggestion.source_run_id ? `run ${shortRunId(suggestion.source_run_id)}` : null,
  ].filter(Boolean)

  return parts.length > 0 ? parts.join(' / ') : suggestion.source ?? null
}

export default function PendingMemorySuggestionCard({
  suggestion,
  isEditing,
  editedContent,
  rejectionReason,
  isApprovePending,
  isRejectPending,
  onApprove,
  onToggleEdit,
  onApproveEdited,
  onEditedContentChange,
  onApproveAndReplace,
  onReject,
  onRejectionReasonChange,
}: PendingMemorySuggestionCardProps) {
  const source = sourceSummary(suggestion)

  function handleEditedContentChange(event: ChangeEvent<HTMLTextAreaElement>) {
    onEditedContentChange(suggestion.id, event.target.value)
  }

  function handleRejectionReasonChange(event: ChangeEvent<HTMLInputElement>) {
    onRejectionReasonChange(suggestion.id, event.target.value)
  }

  return (
    <article className="grid gap-4 rounded-lg border p-4">
      <div className="grid gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            variant="outline"
            className="border-amber-200 bg-amber-50 text-amber-900"
          >
            Not used yet
          </Badge>
          <Badge variant="outline">pending</Badge>
          <Badge variant="outline">{suggestion.category}</Badge>
          <Badge variant="outline">{suggestion.scope}</Badge>
          {suggestion.risk_level && (
            <Badge variant="outline">risk {suggestion.risk_level}</Badge>
          )}
          <span className="text-xs text-muted-foreground">
            priority {suggestion.priority}
          </span>
        </div>

        <div>
          <p className="text-xs font-medium text-muted-foreground">
            Suggested memory
          </p>
          <p className="mt-1 text-sm leading-6">{suggestion.content}</p>
        </div>

        <p className="rounded border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-900">
          Not used yet - Review before approving.
        </p>
      </div>

      {(source || suggestion.rationale) && (
        <div className="grid gap-2 rounded-lg bg-muted/30 p-3 text-xs text-muted-foreground">
          {source && (
            <p>
              <span className="font-medium text-foreground">Source:</span>{' '}
              {source}
            </p>
          )}
          {suggestion.rationale && (
            <p>
              <span className="font-medium text-foreground">Why suggested:</span>{' '}
              {suggestion.rationale}
            </p>
          )}
        </div>
      )}

      {(suggestion.evidence_path || suggestion.evidence_excerpt) && (
        <div className="grid gap-1 rounded-lg bg-muted/30 p-3 text-xs text-muted-foreground">
          {suggestion.evidence_path && (
            <p className="font-mono">
              Found in: {suggestion.evidence_path}
            </p>
          )}
          {suggestion.evidence_excerpt && (
            <p>{suggestion.evidence_excerpt}</p>
          )}
        </div>
      )}

      <div className="grid gap-2">
        <p className="text-xs font-semibold">Review before approving</p>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => onToggleEdit(suggestion)}
            title="Change the wording first. Your edited version is what gets saved."
          >
            {isEditing ? 'Cancel edit' : 'Edit, then approve'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => onApprove(suggestion)}
            disabled={isApprovePending}
            title="Turns this suggestion into active memory the AI may be shown in future runs."
          >
            Approve & start using
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onApproveAndReplace(suggestion)}
            title="Starts using this memory and stops using an older one you choose. The old one is kept in history."
          >
            Approve & replace an old memory
          </Button>
        </div>
      </div>

      {isEditing && (
        <div className="grid gap-2 rounded-lg bg-muted/30 p-3">
          <Label htmlFor={`edit-suggestion-${suggestion.id}`}>
            Memory note wording
          </Label>
          <Textarea
            id={`edit-suggestion-${suggestion.id}`}
            value={editedContent}
            maxLength={400}
            rows={3}
            onChange={handleEditedContentChange}
          />
          <div className="flex justify-end">
            <Button
              size="sm"
              variant="outline"
              onClick={() => onApproveEdited(suggestion)}
              disabled={isApprovePending}
              title="Change the wording first. Your edited version is what gets saved."
            >
              Approve edited memory
            </Button>
          </div>
        </div>
      )}

      <details className="rounded-lg bg-muted/30 p-3">
        <summary className="cursor-pointer text-xs font-semibold">
          Reject suggestion
        </summary>
        <div className="mt-3 grid gap-2">
          <Label htmlFor={`reject-suggestion-${suggestion.id}`}>
            Rejection reason
          </Label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              id={`reject-suggestion-${suggestion.id}`}
              value={rejectionReason}
              onChange={handleRejectionReasonChange}
              placeholder="Why this should not become memory."
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => onReject(suggestion)}
              disabled={isRejectPending}
              title="Dismisses this suggestion. It will not be used."
            >
              Reject suggestion
            </Button>
          </div>
        </div>
      </details>
    </article>
  )
}
