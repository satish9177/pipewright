from backend.db.database import init_db
from backend.memory.memory_store import add_fact, load_hard_facts, flag_stale_memories
from backend.checkpoint.checkpoint_store import save_checkpoint, load_last_checkpoint

init_db()

# Test memory store
add_fact("Tech stack: Python FastAPI", "manual", "founder")
add_fact("Database: SQLite via SQLAlchemy", "manual", "founder")
add_fact("All IDs are UUIDs", "manual", "founder")

facts = load_hard_facts()
print("=== HARD FACTS ===")
print(facts)
assert "FastAPI" in facts

stale = flag_stale_memories(days=90)
print(f"Stale memories flagged: {stale}")

# Test checkpoint store
cp = save_checkpoint(
    run_id="test-run-001",
    step="plan",
    output={"goal": "test goal"},
    handoff_contract={"handoff_from": "planner"},
    git_hash="abc123",
    tests_passed=True
)
print(f"Checkpoint saved: {cp}")

loaded = load_last_checkpoint("test-run-001")
print(f"Checkpoint loaded: {loaded['step']}")
assert loaded["step"] == "plan"

print("=== ALL DAY 1 TESTS PASSED ===")
