import { useQuery } from '@tanstack/react-query'

import { runsApi } from '@/api/client'

interface UseRunTimelineOptions {
  refetchActive?: boolean
}

export default function useRunTimeline(
  runId: string | undefined,
  options: UseRunTimelineOptions = {},
) {
  return useQuery({
    queryKey: ['runTimeline', runId],
    queryFn: () => runsApi.getRunTimeline(runId!),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: options.refetchActive ? 2000 : false,
  })
}
