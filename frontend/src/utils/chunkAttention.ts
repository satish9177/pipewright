import type { ChunkStatus } from '@/api/client'
import {
  parsePatchFailureSummary,
  parseRecoveredPatchReviewSummary,
} from '@/utils/patchFailure'

// #36C: chunk statuses that mean a chunk is currently working or needs attention.
// 'in_progress' is included defensively even though the typed enum uses 'running'.
// Lives in a non-component module so it can be shared without tripping
// react-refresh (#37D2).
export const ACTIVE_ATTENTION_STATUSES = new Set<string>([
  'running',
  'in_progress',
  'failed',
  'awaiting_chunk_approval',
])

// #36D: whether a chunk must stay expanded by default — i.e. it is the active
// chunk or carries a pending decision / recovery / control. Pure; reads existing
// data only. Chunks where this is true render the full ChunkCard and are never
// collapsible, so a chunk that needs attention can never become invisible.
//
// #37D2: this is the single source of truth for "does this chunk need attention?"
// Both ChunkPlanPanel (chunk-level collapse) and RunDetailPage (page-level chunk-
// history collapse) call it, so the page-level collapse stays fail-open and the
// two can never drift. Pass activeChunkNumber = null to ask only about real
// attention signals (ignoring "is this the current chunk").
export function chunkNeedsAttention(
  chunk: ChunkStatus,
  activeChunkNumber: number | null
): boolean {
  if (activeChunkNumber !== null && chunk.chunk_number === activeChunkNumber) {
    return true
  }
  // running / in_progress / failed / awaiting_chunk_approval
  if (ACTIVE_ATTENTION_STATUSES.has(chunk.status)) return true
  if (chunk.pending_scope_expansion) return true
  if (chunk.error_message) return true
  if (parsePatchFailureSummary(chunk.completion_summary)) return true
  if (parseRecoveredPatchReviewSummary(chunk.completion_summary)) return true
  return false
}
