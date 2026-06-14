import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { memoryApi } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

interface RunMemorySuggestionsDigestProps {
  runId: string
}

export default function RunMemorySuggestionsDigest({
  runId,
}: RunMemorySuggestionsDigestProps) {
  const navigate = useNavigate()
  const digestQuery = useQuery({
    queryKey: ['run-memory-suggestions-digest', runId],
    queryFn: () => memoryApi.getRunMemorySuggestions(runId),
    enabled: Boolean(runId),
  })

  const digest = digestQuery.data
  if (
    digestQuery.isLoading ||
    digestQuery.isError ||
    !digest ||
    digest.pending_count <= 0 ||
    digest.suggestions.length === 0
  ) {
    return null
  }

  const suggestionLabel =
    digest.pending_count === 1 ? 'suggestion' : 'suggestions'

  return (
    <Card className="mb-6 border-slate-200 bg-slate-50">
      <CardHeader>
        <CardTitle className="text-base text-slate-900">
          Memory suggestions from this run
        </CardTitle>
        <CardDescription className="text-slate-700">
          This run produced {digest.pending_count} memory {suggestionLabel} that
          are pending your review. They're advisory only and won't be used by
          future AI runs unless you approve them in Project Memory. Nothing was
          added to memory automatically.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button
          variant="outline"
          size="sm"
          className="border-slate-300 bg-white text-slate-900 hover:bg-slate-100"
          onClick={() => navigate(`/memory?projectId=${digest.project_id}`)}
        >
          Review in Project Memory →
        </Button>
      </CardContent>
    </Card>
  )
}
