\# Run Detail Visual Parity Smoke Checklist



\## Purpose



This checklist closes out the Run Detail visual parity work after:



\- #35 Run Detail guided cockpit

\- #36 Active Chunk / ChunkPlanPanel guided UX

\- #37 Visual alignment with Claude Design

\- Post-smoke retry affordance parity fix

\- #38A Run Detail visual system polish



The goal is to verify that the Run Detail UI now feels closer to the Claude Design guided cockpit while preserving Pipewright's safety guarantees.



This is a smoke checklist only. It does not introduce new behavior.



\## Scope



Covered:



\- Tier 1 Run Detail cockpit spine visual treatment

\- Operator attention mood bar

\- Runtime test validation big-number verdict

\- Reviewer independence disclosure

\- Pipeline rail visual polish

\- Existing safety gate visibility

\- Existing action/control parity



Not covered:



\- Full state-gated Tier 2 reorganization

\- ChunkPlanPanel decomposition

\- Approval diff relocation

\- Dark evidence pane redesign

\- New action wiring

\- Global theme/token migration



\## Validation Commands



Run from the frontend directory when frontend files changed:



```bash

npm.cmd run build

