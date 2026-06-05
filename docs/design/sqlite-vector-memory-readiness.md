# SQLite Vector Memory Readiness

> Design ID: **#32G**. Docs-only readiness design. This document does not
> implement SQLite vector memory, pgvector, embeddings, schema changes, package
> changes, or runtime behavior changes.

## 1. Executive Verdict

SQLite vector / FTS / hybrid search is a reasonable future path for
local/open-source semantic memory. It should be optional, local-first, and not
required for quickstart.

PostgreSQL + pgvector remains the hosted/team production path. SQLite vector
support is for trusted local use and should not become the production/team
semantic-memory default.

This design does not implement anything. It records the shape and safety
contract for a future local semantic memory layer.

## 2. Memory Modes

| Mode                  | Storage                                       | Capability                       |
| --------------------- | --------------------------------------------- | -------------------------------- |
| Local quickstart      | SQLite                                        | hard facts / current memory only |
| Local advanced future | SQLite + FTS/vector/hybrid                    | optional semantic memory         |
| Hosted/team future    | PostgreSQL + pgvector                         | production semantic memory       |
| Enterprise future     | PostgreSQL + pgvector + audit/export controls | compliance-ready memory          |

## 3. Current Memory Architecture to Preserve

Current memory is stored in the Pipewright SQLite database. The schema source of
truth is `backend/db/schema.sql`.

### `memory_facts`

`memory_facts` is the canonical table for approved project memory. Current
columns are:

- `id`
- `project_id`
- `content`
- `category`
- `scope`
- `priority`
- `source`
- `added_by`
- `approved_by`
- `approved_at`
- `last_verified_at`
- `created_at`
- `updated_at`
- `is_stale`
- `status`
- `archived_reason`
- `content_hash`

Current fact statuses allowed by `backend/memory/memory_store.py` are `active`,
`stale`, `archived`, and `historical`.

`project_id` is intentionally nullable in the schema for legacy/pre-M1
compatibility, but the application layer requires a non-blank project id for
reads and writes. Unscoped legacy active facts are archived on access and are not
injected into prompts.

Prompt memory currently loads only project-scoped facts where
`project_id = :project_id`, `is_stale = 0`, and `status = 'active'`.
`backend/memory/prompt_builder.py` also applies role category filtering, scope
preference, priority ordering, and conservative token budgets.

### `memory_suggestions`

`memory_suggestions` is the table for proposed memory that is not yet trusted.
Current columns are:

- `id`
- `project_id`
- `content`
- `category`
- `scope`
- `priority`
- `source`
- `evidence_path`
- `evidence_excerpt`
- `status`
- `created_at`
- `updated_at`
- `approved_by`
- `approved_at`
- `rejected_by`
- `rejected_at`
- `rejection_reason`
- `content_hash`
- `source_run_id`
- `source_chunk_number`
- `source_type`
- `source_ref`
- `rationale`
- `suggested_by`
- `risk_level`
- `edited_content`
- `approved_fact_id`

Current suggestion statuses allowed by `backend/memory/bootstrap.py` are
`pending`, `approved`, `rejected`, and `archived`.

Suggestions become facts only through explicit approval. Approval validates the
content, inserts an active fact, and updates the suggestion status in one
transaction. Rejection records `rejected_by`, `rejected_at`, and
`rejection_reason`.

Current dedupe behavior is project-scoped by `content_hash`. Active facts and
pending suggestions suppress duplicate pending suggestions. Run-outcome
suggestions also suppress recreating the same suggestion for the same
`source_run_id` after it was pending, approved, or rejected.

### Current Provenance and Lifecycle

Current provenance fields include `source`, `added_by`, `approved_by`,
`approved_at`, `last_verified_at`, `evidence_path`, `evidence_excerpt`,
`source_run_id`, `source_chunk_number`, `source_type`, `source_ref`,
`rationale`, `suggested_by`, `risk_level`, and `approved_fact_id`.

Stale/archive lifecycle is represented by `status`, `is_stale`,
`last_verified_at`, `updated_at`, and `archived_reason`. `mark_fact_stale`
sets `status = 'stale'`, `is_stale = 1`, and may store the reason in the
existing `archived_reason` field. Archive sets `status = 'archived'` and
`is_stale = 1`.

Current limitations to preserve in design decisions:

- There is no semantic memory table today.
- There is no embedding index today.
- There is no memory embedding provider selection today.
- There is no `memory_embeddings` table today.
- Stale reasons currently reuse `archived_reason`; there is no dedicated
  stale-reason column.
- SQLite local memory is currently advisory hard facts plus pending suggestions,
  not semantic retrieval.

## 4. Future Local Semantic Memory Architecture

Future local semantic memory should be an additive layer, not a replacement for
the current facts/suggestions lifecycle.

Canonical facts should remain in `memory_facts`. Pending, rejected, and approved
proposal flow should remain in `memory_suggestions`. A future semantic layer can
add a derived index table, for example `memory_embeddings`, but that table should
not be the source of truth.

A future embedding/index table should track enough metadata to rebuild, audit,
and migrate the derived index:

- `fact_id`
- `project_id`
- `embedding_backend`
- `embedding_model`
- `embedding_version`
- `embedded_at`
- `content_hash`
- vector dimensions

Local mode should support FTS/keyword search even when vector support is not
available. If a SQLite vector extension is available, vector ranking can be
added. Hybrid search can combine keyword matches, vector similarity, recency,
status/trust signals, role filters, and current project scope.

## 5. Retrieval Safety Contract

This is the hard safety contract for any future semantic retrieval.

- Filter by `project_id` before retrieval/ranking.
- Exclude archived/rejected memory.
- Exclude stale memory, or down-rank it only under an explicit future policy.
- Never retrieve memory across projects.
- Never let memory override the explicit user request.
- Never let memory expand approved file scope.
- Never let memory bypass approval gates.
- Never let memory override current repo reality.
- Current code/index beats old memory.
- User instruction beats memory.
- Safety rules beat memory.
- For vector retrieval, apply safety filters before or together with vector
  ranking, never after blindly selecting top-k.

Memory is advisory context. It must never become an authority channel for
scope, approval, Git, merge, provider, or safety decisions.

## 6. Memory Poisoning Protections

- AI-suggested memory must remain pending until human approval.
- Repo-derived observations are untrusted until approved.
- Rejected suggestions should not repeatedly return.
- Stale memories should be flagged and reviewed.
- Conflicting memories should pause or warn, not silently inject both.
- Secrets, credentials, tokens, PII, stack traces containing secrets, and `.env`
  contents must never be embedded.
- Embeddings themselves may leak sensitive meaning, so secret scanning must
  happen before embedding.

The current content gate already blocks many unsafe memory entries, including
secret-like tokens, credentials, emails, phone numbers, payment card numbers,
prompt-injection markers, control-plane bypass instructions, absolute local
paths, raw stack traces, and large code blocks. A future embedding path must use
the same or stronger gate before writing any embedding.

## 7. Role-Aware Retrieval

Future retrieval should remain role-aware:

- planner gets architecture/product constraints
- coder gets coding conventions and file-scope-relevant facts
- reviewer gets previous bugs, rejected approaches, security/testing patterns
- tester gets test command patterns and flaky-test notes
- PR/status flows get only relevant GitHub/project config facts

This is design-only. No role retrieval implementation changes are made in #32G.

## 8. SQLite Vector Backend Options

Future local semantic memory can use these backend options at design level:

- SQLite FTS5 for keyword/local search.
- sqlite-vec or a similar optional SQLite extension for vector search.
- Hybrid retrieval using both keyword and vector signals.
- Pure SQLite fallback when no vector extension is available.

The vector extension must not be mandatory for quickstart. A first-time local
user should be able to run Pipewright with ordinary SQLite and no embedding
setup.

## 9. Migration Path to pgvector Later

Future retrieval should sit behind a memory-store/retriever interface so local
SQLite, SQLite vector, and hosted PostgreSQL + pgvector can share the same
policy contract.

Metadata should map cleanly from SQLite vector to pgvector:

- `fact_id` and `project_id` preserve source and isolation.
- `embedding_backend`, `embedding_model`, and `embedding_version` support
  provider/model changes and re-embedding.
- `content_hash` detects changed facts and stale embeddings.
- `embedded_at` supports rebuild/audit workflows.
- vector dimensions make compatibility checks explicit.

An export/import path should exist later. Hosted/team mode should use
PostgreSQL + pgvector, not SQLite vector, because hosted/team memory needs
stronger production operations, backup, audit, and team controls.

## 10. Backup/Reset Implications

See `docs/setup/local-state-reset-backup.md` for current local state, reset, and
backup guidance.

Future embeddings/indexes should be treated as derived data. Canonical
`memory_facts` and `memory_suggestions` are the source of truth; an embedding or
FTS index can be rebuilt from approved facts.

Backup implications:

- Back up the SQLite database and `PIPEWRIGHT_ENCRYPTION_KEY` together.
- Never back up secrets into Git.
- Do not commit `.env`, database files, WAL files, journal files, or future
  vector/FTS sidecar/index artifacts.
- A future local vector index may create additional SQLite sidecar/index
  artifacts; backup docs should be updated when that implementation exists.

Reset implications:

- Resetting canonical memory deletes the source of truth unless backed up.
- Resetting only derived embedding/index state should be recoverable by
  rebuilding from canonical facts.
- Rebuild should re-run safety filters and content-hash checks before embedding.

## 11. Non-Goals

- No vector implementation now.
- No schema changes now.
- No embedding provider selection now.
- No pgvector now.
- No LLM-assisted memory rewrite now.
- No automatic memory writes.
- No hosted/team memory implementation now.

## 12. Recommended Future Slices

These should land later than #32:

- Memory retrieval interface audit.
- Secret/PII exclusion hardening for memory ingestion.
- SQLite FTS-only prototype.
- Optional SQLite vector prototype.
- Hybrid retrieval ranking design.
- pgvector production design.
- Memory export/import/admin tooling.
