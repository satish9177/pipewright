import { useQuery } from '@tanstack/react-query'

import { runsApi } from '@/api/client'

export default function useRunTimeline(runId: string | undefined) {
  return useQuery({
    queryKey: ['runTimeline', runId],
    queryFn: () => runsApi.getRunTimeline(runId!),
    enabled: Boolean(runId),
    retry: false,
  })
}
