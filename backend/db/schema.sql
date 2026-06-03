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
    content_hash TEXT
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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_plan_run_id) REFERENCES pipeline_runs(id)
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

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project ON pipeline_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_chunks_run_status ON chunks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_approval_gates_run_status ON approval_gates(run_id, approval_type, status);
CREATE INDEX IF NOT EXISTS idx_scope_expansion_requests_run_chunk_status ON scope_expansion_requests(run_id, chunk_number, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_suggestions_pending_dedupe
ON memory_suggestions(project_id, content_hash)
WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_memory_suggestions_project_status
ON memory_suggestions(project_id, status);
