export const RUN_FLOW_ACTION_LABELS = {
  executeApprovedChunks: 'Execute approved chunks',
  approveChunkPlan: 'Approve chunk plan',
  rejectChunkPlan: 'Reject chunk plan',
  approveChunk: 'Approve chunk',
  rejectChunk: 'Reject chunk',
  approveFinal: 'Approve final',
  rejectFinal: 'Reject final',
  resumeRun: 'Resume run',
  askAiToFixThis: 'Ask AI to fix this',
} as const

export const RUN_FLOW_ACTION_COPY = {
  steerFindingCaption:
    'Sends this suggestion to the AI and re-runs the chunk. Nothing is committed until you review the result.',
  resumeCaption:
    'Continues from the chunk that paused. Nothing is committed without your approval.',
} as const
