# Pipewright Design System

> An AI pipeline that orchestrates multiple models to plan, code, test, and review — with a human approval gate before every merge. **Never autonomous. Human always in control.**

This design system is a brand and UI proposal for **Pipewright**, a developer tool currently shipping as a CLI + REST API and planned for a React/shadcn web UI in Phase 2. The system is grounded in the existing codebase's tone, vocabulary, and pipeline data model — not invented from whole cloth.

## Source

- **Repo:** [github.com/satish9177/pipewright](https://github.com/satish9177/pipewright)
- Read at commit `22e3e7ff`
- Key files mined for voice and contracts: `AGENTS.md`, `DECISIONS.md`, `backend/main.py`, `backend/models/handoff.py`, `backend/pipeline/approval_gate.py`, `backend/db/schema.sql`

The repo is backend-only at time of writing. `frontend/` contains a `.gitkeep` and is reserved for the Phase 2 React + shadcn/ui build. Everything visual in this system is **proposed**, not extracted — see [Caveats](#caveats).

> Future contributors: pull the latest from the repo before iterating on the UI kit. Voice, pipeline contracts (`PlannerHandoff`, `CoderHandoff`, `PatchResult`, `TestResult`, `ApprovalRequest`), and the SQL schema (`pipeline_runs`, `approval_gates`, `checkpoints`, `memory_facts`) are the authoritative inputs.

---

## Product context

Pipewright orchestrates a five-stage AI pipeline:

```
Feature Request → planner → coder → patch_applier → tester → approval
```

Each stage hands off a typed Pydantic contract to the next. The pipeline **pauses** at the approval gate — a human must hit `POST /gates/{id}/approve` (or `/reject`) before anything merges. The gate displays a diff, AI summary, test results, and a `risk_level` (low / medium / high).

There is one product surface today and one planned:

| Surface              | Status      | Description                                              |
| -------------------- | ----------- | -------------------------------------------------------- |
| **Pipewright CLI / API** | Built (MVP) | FastAPI app on port 8001. CLI prompts + curl approvals.  |
| **Pipewright Console**   | Planned     | React/shadcn dashboard. Runs list, gate inspector, memory editor. |

The UI kit in this design system targets the planned **Console**.

---

## Content fundamentals

How copy is written across the product, terminal output, and proposed UI.

### Voice

Pragmatic. Declarative. Slightly rule-bound. Reads like an engineer's lab notebook crossed with a CI log. The system speaks in **imperatives and facts**, not in cheerful product copy.

| Do                                                  | Don't                                              |
| --------------------------------------------------- | -------------------------------------------------- |
| `Never autonomous. Human always in control.`        | `Smarter merges, powered by AI ✨`                  |
| `Pipeline paused — human decision needed`          | `Hey there! Got a sec to review something?`        |
| `Tests: 14 passed, 0 failed`                        | `Looks like everything's green! 🎉`                 |
| `Approval timed out`                                | `Looks like we lost you...`                        |

### Casing & punctuation

- **Bracketed status tags** are the brand's signature: `[OK]`, `[FAIL]`, `[DONE]`, `[ERROR]`, `[APPROVAL]`, `[PIPELINE]`, `[PLANNER]`. From `AGENTS.md`: _"Use plain text only: [OK], [DONE], [FAIL], [ERROR]"_ — Windows shell compatibility is a real constraint that the brand has embraced as visual identity.
- **ALL-CAPS section headers** in terminal output: `TEST RESULTS:`, `DIFF SUMMARY:`, `AI SUMMARY:`. Carry into the UI as monospace eyebrows.
- **Sentence case** for everything else — UI labels, buttons, table headers.
- **No emoji.** Anywhere. The codebase explicitly forbids non-ASCII in print statements.
- **No exclamation marks.** Confident, not enthusiastic.
- **Lowercase code-style identifiers** when referenced inline: `run_id`, `feature_description`, `tests_passed`, `pre-merge`.

### Person

- **Imperative** for instructions: _"Approve this gate"_, _"Reject and provide reason"_.
- **Third person, declarative** for status: _"Pipeline paused"_, _"Gate timed out"_.
- Avoid "you" / "we" unless explaining a rule to the reader (docs).

### Numbers, units, IDs

- Run IDs and gate IDs are full UUIDs in mono — never truncated in the data, but UI may show first 8 chars + `…` in tables.
- Durations: `12.4 seconds`, `30 minutes`, never `12.4s` or `30m`.
- Counts: `14 passed, 0 failed` — full words.
- Token usage: `input=1840 | output=512` — terminal log style, exposed in dev tools.

### Microcopy patterns

| Pattern               | Example                                                         |
| --------------------- | --------------------------------------------------------------- |
| Empty state           | `No runs yet. Start one with POST /run.`                        |
| Loading               | `Waiting for decision...`                                       |
| Confirmation          | `Approve and merge?`                                            |
| Destructive confirm   | `Reject this gate? Provide a reason.`                           |
| Error                 | `planner.py: Gemini failed to return valid plan after 2 attempts.` (module prefix, machine-readable) |

---

## Visual foundations

How the brand looks. Every choice below is rooted in "engineered ledger" — industrial blueprint paper × developer terminal × audit log.

### Color

A **two-surface** palette: warm paper for the app shell, near-black ink for terminal/diff panes. A single warm accent — **solder copper** — appears on primary actions, run-status indicators, and the wordmark mark. Status colors are muted and earthy, not neon.

- **Paper** `#F6F4EE` — main background. Warm, slightly aged.
- **Ink** `#0E1116` — primary text and inverted terminal panes.
- **Copper** `#B7531C` — single accent. Used sparingly: primary button, brand mark, active link.
- **Status** — pass / wait / fail / info each have 600 (default), 100 (soft background), 50 (subtle).

See `colors_and_type.css` for the full token list.

### Typography

- **IBM Plex Sans** for UI, prose, and headings. Engineering pedigree, geometric but warm, free.
- **IBM Plex Mono** for code, diffs, status tags, run IDs, eyebrows, captions, timestamps.
- The mono/sans pairing is intentional and load-bearing: anything that comes from the system (IDs, statuses, durations) is mono; anything written by a human (titles, descriptions, prose) is sans.
- Loaded from Google Fonts (`@import` in `colors_and_type.css`). _Substitution flag:_ no font files exist in the source repo; IBM Plex is the proposed family. If the user prefers a different display family, swap `--pw-font-sans`.

### Spacing & layout

- 4px grid. `--pw-space-1` through `--pw-space-16`.
- Dense by default — this is a tool for engineers, not a marketing site. Default row height 36–40px in tables.
- Generous **horizontal hairline rules** separate sections (`border-bottom: 1px solid var(--pw-border)`). Vertical rules used sparingly to split status columns.
- Layout is grid-based and orthogonal. No diagonal elements, no asymmetric splash compositions.

### Backgrounds

- **Default** — flat paper `#F6F4EE`. No gradient. No texture image.
- **Sunken sections** (memory editor, log viewer) — slightly darker paper `#EFEBE0`.
- **Terminal / diff panes** — flat ink `#0E1116`.
- **Optional blueprint grid** — 1px steel-200 lines on a 24px grid, 8% opacity, behind hero areas only. Use rarely. CSS:
  ```css
  background-image:
    linear-gradient(to right, rgba(181,186,194,.08) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(181,186,194,.08) 1px, transparent 1px);
  background-size: 24px 24px;
  ```

### Borders

- **Hairline** `1px solid #D6D9DE` is the workhorse — cards, inputs, table cells.
- **Strong** `1px solid #0E1116` for emphasis (active tab, focused input).
- **Dashed** `1px dashed #B5BAC2` for empty states and pending placeholders only.
- Border radii are tiny: `2px` for buttons/inputs, `4px` for cards, `0` for tables, `999px` (pill) **only** for status tags.

### Shadows / elevation

Pipewright **avoids drop shadows**. Lift is communicated through:

- Hairline border + slight tone shift (paper-2 vs paper)
- Inset 1px highlight on press (`--pw-shadow-press`)
- One reluctant exception: dropdowns/popovers get `0 8px 24px -12px rgba(14,17,22,0.18)` — barely there.

No glow effects. No outer shadows on cards.

### Corner radii

- Buttons, inputs, tags-square: `2px`
- Cards, modals: `4px`
- Status pills: `999px` (only place pills appear)
- Diff lines, table cells: `0px`

### Animation

- **Sparse and functional.** Duration `120–200ms`. Easing: `cubic-bezier(0.2, 0, 0, 1)` (Plex-feeling out-curve) or simple `ease-out`.
- **No bounces**, no springs, no parallax, no scroll-jacking.
- **Allowed:** opacity fades on hover (150ms), height transitions on accordion (200ms), the pulsing dot on `running` and `pending` rows (1.2s ease-in-out infinite).
- **Forbidden:** shimmer skeletons (use static `…` instead), confetti, page-load animations, gradient sweeps.

### Hover & press states

- **Hover on neutral surfaces:** `background: var(--pw-steel-50)` (paper) or `rgba(255,255,255,0.04)` (ink).
- **Hover on primary copper:** `background: var(--pw-copper-700)` (darken).
- **Press:** `transform: translateY(0.5px); box-shadow: var(--pw-shadow-press)` — a tactile compress, never a shrink.
- **Focus:** 2px copper outline, 2px offset. Always visible. Never removed.

### Transparency & blur

- Almost never. Status tag backgrounds are solid soft colors, not translucent.
- One exception: modal scrim `rgba(14,17,22,0.5)`. No backdrop-filter.

### Imagery

- The product has no photography. If imagery is needed in marketing contexts:
  - **B&W or duotone** (ink + copper), never full color
  - **Subject:** schematic diagrams, technical drawings, machine parts, fitting diagrams, pipe joints. Never people, never office stock.
- Diagrams within the product are SVG line drawings, 1.5px stroke, no fill, ink color.

### Layout rules

- **App chrome is fixed.** Sidebar (220px) and top bar (48px) never scroll.
- **Content is the only scrollable region.** Sticky table headers within content.
- **Max content width** for prose: 720px. Tables and diff viewers expand to full width.
- **Diff and log views are full-bleed** to the right of the sidebar — never margin-padded.

### Card anatomy

```
+-------------------------------------------+
| [EYEBROW]  in mono, uppercase             |   <- 11px, copper or steel-500
|                                           |
|  Title in sans                            |   <- 16–20px, ink-900
|  Optional supporting line in sans-muted.  |   <- 14px, ink-700
|                                           |
|  [content / chart / list]                 |
|                                           |
|  run_id · 12.4 seconds · just now         |   <- mono caption, 11px, subtle
+-------------------------------------------+
   1px hairline border, 4px radius, paper bg
   no shadow
```

---

## Iconography

The Pipewright codebase ships **zero icons**. There is no icon font, no SVG library, no images. The terminal output uses ASCII tags exclusively (`[OK]`, `[FAIL]`, etc.) as a deliberate constraint.

This design system proposes:

- **Lucide** ([lucide.dev](https://lucide.dev)) as the icon set — `1.5px` stroke, square caps, neutral. Linked from CDN: `https://unpkg.com/lucide@latest`. Substitution flagged: this is a proposal, not an extraction.
- **Icons only where strictly needed:** sidebar nav, table row status, button affordances. Default to **text labels** before reaching for a glyph.
- **Logo / wordmark:** custom, generated as SVG in `assets/`. The mark is a **bracket-and-pipe** symbol — `[⊢]` style — riffing on the bracketed-tag voice from terminal output. See `assets/pipewright-mark.svg`.
- **No emoji.** Never. Not in product, not in marketing, not in docs.
- **No unicode glyph icons** (✓, ✗, →, etc.) — they violate the Windows-compat rule the project chose to internalize.

When in doubt, the system uses a **bracketed text token** instead of an icon: `[PASS]` instead of ✓, `[FAIL]` instead of ✗.

---

## Index

| Path                       | What                                                          |
| -------------------------- | ------------------------------------------------------------- |
| `README.md`                | This file                                                     |
| `SKILL.md`                 | Skill entry-point for Claude Code / agent use                 |
| `colors_and_type.css`      | Full token set — colors, type, spacing, radii, status         |
| `assets/`                  | Logos, wordmark, brand marks (SVG)                            |
| `preview/`                 | Design system cards rendered in the Design System tab         |
| `ui_kits/console/`         | Proposed React/shadcn console for Phase 2 (interactive demo)  |

### UI Kits

- `ui_kits/console/` — **Pipewright Console**. The planned Phase 2 web UI. Runs list, gate inspector with diff & approve/reject flow, memory editor, run detail with pipeline stages.

---

## Caveats

1. **Frontend is greenfield.** The repo has no UI code. This system is a proposal based on the backend's voice, pipeline contracts, and explicit Phase 2 stack choice (`React 18 / TypeScript / shadcn/ui`). Visual decisions should be validated with the maintainer before locking.
2. **Fonts substituted.** No font files exist in the source. IBM Plex Sans/Mono is the proposed family — free, engineering-coded, free via Google Fonts. Loaded via `@import`.
3. **Icons substituted.** No icon library exists in source. Lucide is the proposed set. The system actively discourages icon use in favor of text tags.
4. **Brand mark is original.** No logo exists in the source repo. The bracket-and-pipe wordmark in `assets/` is generated and should be reviewed.
5. **Risk levels** — the data model includes `low / medium / high` but the codebase only emits `medium`. The UI design assumes all three will eventually be used.
