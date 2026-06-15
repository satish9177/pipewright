/**
 * scopeWarnings.ts
 * Frontend helper for surfacing backend file-scope warnings (#22B).
 *
 * #22A appends deterministic file-scope notes to a chunk's `rationale`, each
 * prefixed with the `[SCOPE]` marker (e.g. when planner prose references a file
 * outside `files_expected`, or a hard allowlist caps/drops files). This helper
 * extracts those note lines so the approval UI can show them as a warning
 * banner. It is read-only display logic: it never changes scope or data.
 */

export const SCOPE_MARKER = '[SCOPE]'
export const SIZE_MARKER = '[SIZE]'

export const KNOWN_NOTE_MARKERS = [SCOPE_MARKER, SIZE_MARKER] as const

function findNextMarkerIndex(text: string, startIndex: number): number {
  let nextIndex = -1
  for (const marker of KNOWN_NOTE_MARKERS) {
    const markerIndex = text.indexOf(marker, startIndex)
    if (markerIndex !== -1 && (nextIndex === -1 || markerIndex < nextIndex)) {
      nextIndex = markerIndex
    }
  }
  return nextIndex
}

/**
 * Extract the individual marker notes from one or more text fields.
 *
 * The backend joins multiple notes as `[SCOPE] a [SIZE] b`, so each segment is
 * captured only until the next known marker. Any leading unmarked rationale text
 * before the first target marker is ignored.
 */
export function extractMarkerNotes(
  marker: (typeof KNOWN_NOTE_MARKERS)[number],
  ...texts: Array<string | null | undefined>
): string[] {
  const warnings: string[] = []
  const seen = new Set<string>()

  for (const text of texts) {
    if (!text) continue
    let markerIndex = text.indexOf(marker)
    while (markerIndex !== -1) {
      const noteStart = markerIndex + marker.length
      const nextMarkerIndex = findNextMarkerIndex(text, noteStart)
      const noteEnd = nextMarkerIndex === -1 ? text.length : nextMarkerIndex
      const note = text.slice(noteStart, noteEnd).trim()
      if (note && !seen.has(note)) {
        seen.add(note)
        warnings.push(note)
      }
      markerIndex = text.indexOf(marker, noteStart)
    }
  }

  return warnings
}

/**
 * Extract the individual `[SCOPE]` warning notes from one or more text fields
 * (typically a chunk's rationale, and optionally its description).
 */
export function extractScopeWarnings(
  ...texts: Array<string | null | undefined>
): string[] {
  return extractMarkerNotes(SCOPE_MARKER, ...texts)
}

export function extractSizeWarnings(
  ...texts: Array<string | null | undefined>
): string[] {
  return extractMarkerNotes(SIZE_MARKER, ...texts)
}
