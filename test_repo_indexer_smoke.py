from backend.config.keys import settings
from backend.db.database import init_db
from backend.repo.repo_indexer import (
    build_repo_index,
    get_file_index_count,
    get_relevant_files,
)

init_db()

if not settings.target_repo_path:
    raise RuntimeError(
        "test_repo_indexer_smoke.py: TARGET_REPO_PATH is required for smoke test"
    )

PROJECT_ID = "smoke-target"

result = build_repo_index(PROJECT_ID, settings.target_repo_path)
print(result)

count = get_file_index_count()
print("Files indexed:", count)

matches = get_relevant_files(PROJECT_ID, "planner service route", limit=10)
print("Relevant files:")
for f in matches:
    print(f["path"], f["file_type"], f["token_estimate"])

assert count > 0
print("REPO INDEXER SMOKE TEST PASSED")
