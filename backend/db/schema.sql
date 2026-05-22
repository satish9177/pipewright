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

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    feature_description TEXT NOT NULL,
    plain_english_summary TEXT,
    status TEXT DEFAULT 'running',
    current_step TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT DEFAULT 'complete',
    output TEXT,
    handoff_contract TEXT,
    git_commit_hash TEXT,
    tests_passed INTEGER DEFAULT 0,
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
    rejection_reason TEXT,
    decided_at DATETIME,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);
