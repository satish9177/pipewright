CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source TEXT,
    added_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME,
    is_stale INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    archived_reason TEXT
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
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    feature_description TEXT NOT NULL,
    plain_english_summary TEXT,
    status TEXT DEFAULT 'running',
    current_step TEXT,
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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
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
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
    UNIQUE(run_id, chunk_number)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project ON pipeline_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_chunks_run_status ON chunks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_approval_gates_run_status ON approval_gates(run_id, approval_type, status);
