---
name: pipewright-design
description: Use this skill to generate well-branded interfaces and assets for Pipewright, either for production or throwaway prototypes/mocks/etc. Pipewright is an AI pipeline that orchestrates models to plan, code, test, and review — with a mandatory human approval gate before every merge. Contains essential design guidelines, colors, type, fonts, assets, and a UI kit for the proposed Phase 2 console.
user-invocable: true
---

Read the `README.md` file within this skill, and explore the other available files. The README covers content fundamentals (voice, casing, tone), visual foundations (colors, type, spacing, motion), and iconography. The full token set lives in `colors_and_type.css`.

If creating visual artifacts (slides, mocks, throwaway prototypes), copy assets out of `assets/` and create static HTML files for the user to view. Reference the components in `ui_kits/console/` for component patterns (buttons, status pills, risk badges, diff views, approval gates).

If working on production code, copy the CSS variables from `colors_and_type.css` and read the rules here to become an expert in designing with the Pipewright brand. The brand voice is distinctive and load-bearing: bracketed ASCII status tags (`[OK]`, `[FAIL]`, `[APPROVAL]`), no emoji, no exclamation marks, declarative imperatives, mono for anything machine-generated.

The source codebase is at <https://github.com/satish9177/pipewright>. Pull the latest before extending the UI kit — pipeline contracts and the SQL schema are the authoritative inputs for any new screen.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions (audience, fidelity, screen vs slide vs prototype, which pipeline screens or marketing artifacts they need), and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

Key constraints — always honor:
- **Never use emoji.** Anywhere.
- **No exclamation marks.** Confident, not enthusiastic.
- **Use mono for IDs, status tags, paths, durations.** Use sans for prose and titles.
- **Bracketed ASCII tags** (e.g. `[APPROVAL]`, `[FAIL]`) are the signature voice — use them liberally where the backend would emit them.
- **No drop shadows on cards.** Lift via hairline border + tone shift.
- **Square or 2px corners on UI.** Pills are reserved for status tags only.
- **Solder copper (`#B7531C`) is the only accent color.** Use sparingly — primary buttons, the brand mark, never on body text.
