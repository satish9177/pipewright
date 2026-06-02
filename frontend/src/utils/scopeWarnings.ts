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

/**
 * Extract the individual `[SCOPE]` warning notes from one or more text fields
 * (typically a chunk's rationale, and optionally its description).
 *
 * The backend joins multiple notes as `[SCOPE] a [SCOPE] b`, so we split on the
 * marker and keep the non-empty, de-duplicated remainders. Any leading
 * non-scope rationale text (before the first marker) is ignored.
 */
export function extractScopeWarnings(
  ...texts: Array<string | null | undefined>
): string[] {
  const warnings: string[] = []
  const seen = new Set<string>()

  for (const text of texts) {
    if (!text) continue
    const markerIndex = text.indexOf(SCOPE_MARKER)
    if (markerIndex === -1) continue

    const notesSection = text.slice(markerIndex)
    for (const piece of notesSection.split(SCOPE_MARKER)) {
      const note = piece.trim()
      if (note && !seen.has(note)) {
        seen.add(note)
        warnings.push(note)
      }
    }
  }

  return warnings
}
