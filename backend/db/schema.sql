-- memory_facts.project_id is intentionally nullable for legacy/pre-M1
-- compatibility. The application layer (memory_store.py) requires a non-blank
-- project_id for every read and write, and any unscoped legacy rows are
-- archived and never injected into prompts (see _archive_unscoped_pre_m1_memory).
-- A future migration may tighten this to NOT NULL after a local-DB backfill
-- strategy exists. Do not change it here without that backfill.
CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    content TEXT NOT NULL,
    category TEXT DEFAULT 'other',
    scope TEXT DEFAULT 'global',
    priority INTEGER DEFAULT 100,
    source TEXT,
    added_by TEXT,
    approved_by TEXT,
    approved_at DATETIME,
    last_verified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME,
    is_stale INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    archived_reason TEXT,
    content_hash TEXT,
    superseded_by_fact_id TEXT
);

CREATE TABLE IF NOT EXISTS memory_suggestions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL,
    priority INTEGER DEFAULT 100,
    source TEXT DEFAULT 'bootstrap',
    evidence_path TEXT,
    evidence_excerpt TEXT,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME,
    approved_by TEXT,
    approved_at DATETIME,
    rejected_by TEXT,
    rejected_at DATETIME,
    rejection_reason TEXT,
    content_hash TEXT,
    source_run_id TEXT,
    source_chunk_number INTEGER,
    source_type TEXT,
    source_ref TEXT,
    rationale TEXT,
    suggested_by TEXT,
    risk_level TEXT,
    quality_score INTEGER,
    edited_content TEXT,
    approved_fact_id TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    test_command TEXT NOT NULL,
    branch TEXT DEFAULT 'main',
    description TEXT DEFAULT '',
    github_token TEXT,
    github_owner TEXT,
    github_repo TEXT,
    github_base_branch TEXT DEFAULT 'pipewright-staging',
    pr_mode TEXT DEFAULT 'local_only',
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    feature_description TEXT NOT NULL,
    plain_english_summary TEXT,
    report_json TEXT,
    status TEXT DEFAULT 'running',
    current_step TEXT,
    intent TEXT DEFAULT 'implementation',
    chunk_plan_status TEXT DEFAULT 'none',
    chunk_plan TEXT,
    total_chunks INTEGER DEFAULT 0,
    current_chunk_number INTEGER DEFAULT 0,
    pr_url TEXT,
    pr_number INTEGER,
    branch_name TEXT,
    pushed_at DATETIME,
    pr_created_at DATETIME,
    push_error TEXT,
    source_plan_run_id TEXT,
    start_branch TEXT,
    start_head_sha TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_plan_run_id) REFERENCES pipeline_runs(id)
);

-- plan_versions is the append-only within-run plan version scaffold for §23
-- row 7a. triage_json stores the same plan data/trust class already persisted
-- in pipeline_runs.chunk_plan. This slice only writes version 1 at plan creation
-- time; it does not store diffs, repo file contents, prompts, or secrets beyond
-- what the existing plan JSON already contains, and runtime still reads
-- pipeline_runs.chunk_plan as the live pointer.
CREATE TABLE IF NOT EXISTS plan_versions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    triage_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('initial', 'seeded', 'plan_turn')),
    created_from_turn_id TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT DEFAULT 'complete',
    output TEXT,
    handoff_contract TEXT,
    git_commit_hash TEXT,
    step_completed INTEGER DEFAULT 1,
    tests_passed INTEGER DEFAULT 0,
    chunk_number INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE TABLE IF NOT EXISTS approval_gates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    diff TEXT,
    test_results TEXT,
    ai_summary TEXT,
    plain_english_summary TEXT,
    risk_level TEXT DEFAULT 'medium',
    chunk_number INTEGER DEFAULT 0,
    approval_type TEXT DEFAULT 'legacy',
    rejection_reason TEXT,
    decided_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE TABLE IF NOT EXISTS file_index (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    summary TEXT,
    key_imports TEXT,
    last_modified DATETIME,
    token_estimate INTEGER DEFAULT 0,
    line_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, path)
);

CREATE TABLE IF NOT EXISTS project_index_fingerprints (
    project_id TEXT PRIMARY KEY,
    repo_path_resolved TEXT NOT NULL,
    branch_name TEXT,
    branch_is_detached INTEGER NOT NULL DEFAULT 0,
    detached_head_label TEXT,
    head_sha TEXT NOT NULL,
    dirty_digest TEXT NOT NULL,
    dirty_files_count INTEGER DEFAULT 0,
    index_row_count INTEGER DEFAULT 0,
    captured_at DATETIME NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    snapshot_state TEXT DEFAULT 'current',
    snapshot_reason TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    files_expected TEXT,
    depends_on TEXT,
    risk_level TEXT DEFAULT 'medium',
    token_estimate INTEGER DEFAULT 0,
    requires_human_review INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    previous_chunks_context TEXT,
    completion_summary TEXT,
    error_message TEXT,
    -- Display-only runtime test-validation evidence (#28D). Recorded after the
    -- chunk's test command runs; NEVER gates, blocks, or changes run outcome.
    -- All nullable: chunks that never ran tests (or pre-#28D rows) load as NULL.
    test_run_verdict TEXT,                 -- strong / weak / none / unknown
    test_run_verdict_reason TEXT,
    test_run_command_quality TEXT,         -- weak / likely_test / unknown (#23A)
    test_run_counts_parsed INTEGER,        -- 0/1, nullable
    test_run_zero_tests_detected INTEGER,  -- 0/1, nullable
    test_run_counts_json TEXT,             -- json {total,passed,failed} or null
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
    UNIQUE(run_id, chunk_number)
);

-- chunk_attempts is the append-only execution ledger for Phase 2 item 12. It
-- stores metadata about driver passes only: stage outcome classes, small
-- evidence refs, final outcome/status, the optional item-17a stage profile and
-- eligibility audit flag, and the git HEAD at attempt end. It never stores
-- prompts, diffs, file
-- contents, test output, or secrets. Older runs may have no rows; resume
-- degrades to the legacy checkpoint behavior when no completed attempt HEAD
-- exists.
CREATE TABLE IF NOT EXISTS chunk_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    entry_mode TEXT NOT NULL,
    stage_outcomes_json TEXT,
    evidence_refs_json TEXT,
    final_outcome_class TEXT,
    final_status TEXT,
    stage_profile TEXT,
    trivial_profile_eligible INTEGER,
    head_sha TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
    UNIQUE(run_id, chunk_number, attempt_number)
);

-- run_turns is the append-only conversation/turn log for a run (Phase 3 item 13;
-- proposal §4.3). One row per human steer message: the sanitized steer text, the
-- target type/chunk sentinel, and a link to the chunk_attempts row the turn produced
-- (attempt_id; the attempt is the ledger's job, the message is this table's).
-- The original pipeline_runs.feature_description stays immutable — it is the
-- audit anchor; turns are additive context. The message is advisory only: a turn
-- row never grants scope, approves a gate, or alters Git/merge behavior. This
-- table stores user steer text and metadata ONLY — never diffs, test output,
-- provider/Git errors, prompts, or secrets (same discipline as chunk_attempts).
-- Rows are never updated or deleted by the application. chunk_number is NOT NULL
-- by design; plan turns use target_type='plan' and chunk_number=0.
CREATE TABLE IF NOT EXISTS run_turns (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'chunk',
    chunk_number INTEGER NOT NULL,
    steer_text TEXT NOT NULL,
    attempt_id TEXT,
    outcome TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
    UNIQUE(run_id, turn_number)
);

-- scope_expansion_requests is the authoritative, audited home for human-approved
-- scope amendments (#27). chunks.files_expected stays immutable; effective scope
-- is reconstructed as original files_expected UNION the approved_files of in-force
-- (approved/applied) rows here. No file contents, secrets, or token-like values
-- are ever stored here, only paths, ids, status, audit columns, and a sanitized
-- decision reason. Columns mirror ScopeExpansionRequest in
-- backend/pipeline/scope_expansion.py one-to-one.
CREATE TABLE IF NOT EXISTS scope_expansion_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    failure_report_id TEXT NOT NULL,
    requested_files TEXT,
    approved_files TEXT,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    decided_at DATETIME,
    applied_at DATETIME,
    decided_by TEXT,
    decision_reason TEXT,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

-- test_validation_acknowledgements is the audited home for human acknowledgements
-- of a weak/none runtime test verdict at the final gate (#28F). It mirrors the
-- scope_expansion_requests shape. Each row binds an acknowledgement to the EXACT
-- diff it was made against (acknowledged_diff_hash), so a later retry/amendment
-- that changes the chunk's diff makes the acknowledgement stale and re-requires a
-- fresh one. No file contents, diffs, secrets, or token-like values are stored
-- here — only ids, the verdict, the diff hash, audit columns, and a sanitized
-- human reason. Acknowledgement is a precondition for final approval; it never
-- replaces it and never auto-commits.
CREATE TABLE IF NOT EXISTS test_validation_acknowledgements (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    acknowledged_diff_hash TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at DATETIME,
    reason TEXT,
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

-- review_finding_acknowledgements is the audited home for human
-- acknowledgements of current high-severity reviewer findings in the default
-- ack-required policy categories (Phase 4 item 15). Each row binds a
-- panel-level acknowledgement to the exact chunk diff identity
-- (acknowledged_diff_hash). It stores metadata only: no finding bodies, diffs,
-- provider errors, prompts, file contents, secrets, or token-like values.
-- Acknowledgement is a soft precondition for chunk/final approval; it never
-- approves, rejects, commits, pushes, creates PRs, or changes reviewer output.
CREATE TABLE IF NOT EXISTS review_finding_acknowledgements (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    acknowledged_diff_hash TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at DATETIME,
    reason TEXT,
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

-- chunk_reviews is the isolated, additive home for advisory per-chunk AI reviews
-- (Adversarial Reviewer Stage v1; docs/design/adversarial-reviewer-stage.md). It is
-- deliberately separate from checkpoints (resume/safety substrate) and from
-- chunks.completion_summary (already polymorphic), so advisory LLM evidence never
-- couples into the resume path or patch-failure data. A review is bound to the
-- chunk's test-checkpoint / diff identity (reviewed_test_checkpoint_hash, the #28F
-- hash concept) so review staleness tracks acknowledgement staleness. Reviews are
-- advisory/display-only: they gate nothing, commit nothing, and grant no authority.
-- Only a 'completed' review carries a verdict/findings/summaries; 'failed' and
-- 'unavailable' rows are provably empty. No file contents, secrets, or token-like
-- values are stored here.
CREATE TABLE IF NOT EXISTS chunk_reviews (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    review_status TEXT NOT NULL,             -- completed / failed / unavailable
    verdict TEXT,                            -- null unless completed
    summary TEXT,
    findings_json TEXT,                      -- json array; '[]' when not completed
    test_gap_summary TEXT,
    scope_summary TEXT,
    security_or_safety_summary TEXT,
    recommended_human_action TEXT,
    reviewed_test_checkpoint_hash TEXT,      -- chunk diff identity (#28F concept)
    checkpoint_id TEXT,
    provider TEXT,
    model TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

-- llm_call_provenance is the isolated, additive home for METADATA-ONLY records of
-- which provider/model actually produced a role's effective output for a run/chunk
-- (#33B; docs/design/multi-provider-modes.md). It is deliberately separate from
-- checkpoints (the resume/safety substrate) and from chunks/completion_summary, so
-- provenance never couples into the resume path. It is audit/display-only: it gates
-- nothing, commits nothing, retries nothing, expands no scope, and grants no
-- authority. METADATA ONLY — no prompts, no LLM responses, no diffs, no file
-- contents, no API keys/tokens/auth headers, and no raw provider errors are ever
-- stored here; only provider/model names, the role, an optional selection_source
-- label, finish_reason, token counts, and audit ids/timestamps. chunk_number is
-- nullable for pre-chunk roles. selection_source is nullable: it is reserved for a
-- future slice and is NOT derived here, because deriving it would duplicate or change
-- resolve_role_config precedence (out of scope for #33B).
CREATE TABLE IF NOT EXISTS llm_call_provenance (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    selection_source TEXT,
    finish_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

-- memory_injection_events is the append-only, audit/display-only record of which
-- approved memory facts were injected into a role's prompt for a run/chunk
-- (M3C1; docs/design/memory-m3-trust-lifecycle.md §14). It exists so a human can
-- later see the EXACT memory a role received at execution time, even after the
-- underlying facts are edited, archived, marked stale, or superseded. It is
-- deliberately separate from checkpoints/chunks so provenance never couples into
-- the resume path. It gates nothing, commits nothing, retries nothing, mutates no
-- memory, and grants no authority. MEMORY ENTRIES ONLY — never full prompts, repo
-- files, handoff contracts, source code, API keys, or tokens. entries_json holds
-- {"included":[...],"excluded":[...]} of already-approved (write-gate-validated)
-- memory entries. entries_hash is a deterministic digest of the ordered INCLUDED
-- entries (never the timestamped block header), for drift/integrity checks.
-- chunk_number is nullable for run-level roles (e.g. triage). Append-only: rows
-- are never updated or deleted by the application.
CREATE TABLE IF NOT EXISTS memory_injection_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_number INTEGER,
    role TEXT NOT NULL,
    attempt_number INTEGER DEFAULT 1,
    attempt_id TEXT,
    repo_head_sha TEXT,
    token_budget INTEGER,
    category_policy TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    included_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL,
    entries_hash TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project ON pipeline_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_versions_run_version
ON plan_versions(run_id, version);
CREATE INDEX IF NOT EXISTS idx_chunks_run_status ON chunks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_chunk_attempts_run_chunk ON chunk_attempts(run_id, chunk_number, attempt_number);
CREATE INDEX IF NOT EXISTS idx_chunk_attempts_completed_head ON chunk_attempts(run_id, final_status, chunk_number);
CREATE INDEX IF NOT EXISTS idx_run_turns_run_chunk ON run_turns(run_id, chunk_number, turn_number);
CREATE INDEX IF NOT EXISTS idx_approval_gates_run_status ON approval_gates(run_id, approval_type, status);
CREATE INDEX IF NOT EXISTS idx_project_index_fingerprints_updated ON project_index_fingerprints(updated_at);
CREATE INDEX IF NOT EXISTS idx_scope_expansion_requests_run_chunk_status ON scope_expansion_requests(run_id, chunk_number, status);
CREATE INDEX IF NOT EXISTS idx_test_validation_ack_run_chunk_status ON test_validation_acknowledgements(run_id, chunk_number, status);
CREATE INDEX IF NOT EXISTS idx_review_finding_ack_run_chunk_status ON review_finding_acknowledgements(run_id, chunk_number, status);
CREATE INDEX IF NOT EXISTS idx_chunk_reviews_run_chunk ON chunk_reviews(run_id, chunk_number, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_call_provenance_run_chunk_role ON llm_call_provenance(run_id, chunk_number, role);
CREATE INDEX IF NOT EXISTS idx_memory_injection_events_run_chunk_role ON memory_injection_events(run_id, chunk_number, role);
CREATE INDEX IF NOT EXISTS idx_memory_injection_events_project ON memory_injection_events(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_suggestions_pending_dedupe
ON memory_suggestions(project_id, content_hash)
WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_memory_suggestions_project_status
ON memory_suggestions(project_id, status);
