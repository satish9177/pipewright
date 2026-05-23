// Mock data for the Pipewright Console.
// Modeled directly on backend Pydantic contracts:
//   PlannerHandoff, CoderHandoff, PatchResult, TestResult, ApprovalRequest
// and the SQL schema (pipeline_runs, approval_gates, memory_facts).

window.PW_DATA = {
  runs: [
    {
      id: "8c3f1a92-d4e7-4b8a-9f3c-2e1f4a9b8c7d",
      feature_description: "Add retry logic to the planner when Gemini rate-limits us.",
      status: "paused",
      current_step: "approval",
      created_at: "2026-05-23T14:42:11Z",
      gate_id: "ba8c4179-3f12-4d9e-bc8f-2a1e5d6f4b9c",
      risk_level: "medium",
      duration_seconds: 12.4,
      tests_total: 14,
      tests_passed: 14,
      tests_failed: 0,
      files_changed: ["backend/pipeline/planner.py", "backend/tests/test_planner.py"],
      goal: "Catch 429 errors from Gemini, wait 60 seconds, retry once before failing the run.",
    },
    {
      id: "b4a7f10e-a2c8-4f0d-b3e1-5c8a9d2f1e3b",
      feature_description: "Add token usage logging to every AI call.",
      status: "complete",
      current_step: "done",
      created_at: "2026-05-23T14:30:00Z",
      risk_level: "low",
      duration_seconds: 8.1,
      tests_total: 22,
      tests_passed: 22,
      tests_failed: 0,
      files_changed: [
        "backend/pipeline/planner.py",
        "backend/pipeline/coder.py",
        "backend/utils/json_helpers.py",
      ],
      goal: "Log input_tokens, output_tokens, model, run_id after every Gemini call.",
    },
    {
      id: "7d2c5ab8-9e1f-4a3b-bc2d-8e7f6a5b4c3d",
      feature_description: "Refactor patch_applier to use pathlib instead of os.path.",
      status: "failed",
      current_step: "test",
      created_at: "2026-05-23T13:58:00Z",
      risk_level: "high",
      duration_seconds: 18.7,
      tests_total: 28,
      tests_passed: 24,
      tests_failed: 4,
      files_changed: ["backend/pipeline/patch_applier.py"],
      goal: "Modernize file path handling without changing behavior.",
    },
    {
      id: "1f9e4b2a-c3d8-4e7f-9a8b-7d6c5e4f3a2b",
      feature_description: "Surface memory suggestions in /runs response.",
      status: "complete",
      current_step: "done",
      created_at: "2026-05-23T11:22:00Z",
      risk_level: "low",
      duration_seconds: 5.3,
      tests_total: 12,
      tests_passed: 12,
      tests_failed: 0,
      files_changed: ["backend/main.py", "backend/pipeline/orchestrator.py"],
      goal: "Return suggested_memory_entries on the run status endpoint.",
    },
    {
      id: "3e8d7c6b-5a4f-4e3d-2c1b-0a9f8e7d6c5b",
      feature_description: "Add 30-minute approval timeout enforcement.",
      status: "rejected",
      current_step: "approval",
      created_at: "2026-05-23T10:15:00Z",
      risk_level: "medium",
      duration_seconds: 9.8,
      tests_total: 16,
      tests_passed: 16,
      tests_failed: 0,
      rejection_reason: "Touches approval polling — needs design review first.",
      files_changed: ["backend/pipeline/approval_gate.py"],
      goal: "Mark gate as timeout after 30 minutes of no decision.",
    },
    {
      id: "5a2b1c9d-8e7f-4a3b-2c1d-9e8f7a6b5c4d",
      feature_description: "Pin Gemini model version to module constant.",
      status: "complete",
      current_step: "done",
      created_at: "2026-05-23T09:40:00Z",
      risk_level: "low",
      duration_seconds: 3.2,
      tests_total: 8,
      tests_passed: 8,
      tests_failed: 0,
      files_changed: ["backend/pipeline/planner.py", "backend/pipeline/coder.py"],
      goal: "Replace 'gemini-latest' with hardcoded gemini-2.5-flash constant.",
    },
  ],

  gate: {
    id: "ba8c4179-3f12-4d9e-bc8f-2a1e5d6f4b9c",
    run_id: "8c3f1a92-d4e7-4b8a-9f3c-2e1f4a9b8c7d",
    status: "pending",
    risk_level: "medium",
    ai_summary:
      "Adds 429 rate-limit handling to planner.py. On rate limit, waits 60s and retries once. Failure after retry raises RuntimeError as before. No behavioral change for the success path.",
    diff: `--- a/backend/pipeline/planner.py
+++ b/backend/pipeline/planner.py
@@ -84,12 +84,17 @@ async def run_planner(
     except Exception as unexpected:
         error_str = str(unexpected)
-        raise RuntimeError(
-            f"planner.py: Unexpected error during planning. "
-            f"run_id={run_id} | error={unexpected}"
-        )
+        if "429" in error_str:
+            print(f"[PLANNER] Rate limited by Gemini. Waiting 60 seconds...")
+            await asyncio.sleep(60)
+            print(f"[PLANNER] Retrying after rate limit wait...")
+            try:
+                response = model.generate_content(user_prompt)
+                raw_text = response.text
+                _log_token_usage(response, run_id)
+                handoff = _parse_handoff(raw_text, run_id)
+                print(f"[PLANNER] Plan validated after rate limit retry")
+            except Exception as retry_error:
+                raise RuntimeError(
+                    f"planner.py: Failed after rate limit retry. "
+                    f"run_id={run_id} | error={retry_error}"
+                )
+        else:
+            raise RuntimeError(
+                f"planner.py: Unexpected error during planning. "
+                f"run_id={run_id} | error={unexpected}"
+            )`,
    plan: {
      goal: "Catch 429 errors from Gemini, wait 60 seconds, retry once before failing the run.",
      steps: [
        "Detect 429 in error string inside the unexpected-error handler",
        "Wait 60 seconds using await asyncio.sleep (never time.sleep in async)",
        "Retry the Gemini call once",
        "Raise RuntimeError with module prefix if retry fails",
      ],
      files_to_modify: ["backend/pipeline/planner.py"],
      out_of_scope: ["Changes to coder.py — handled in a separate run"],
      risks: ["Rate-limit retry could double daily quota usage on Gemini free tier"],
    },
    tests: { total: 14, passed: 14, failed: 0, duration_seconds: 12.4 },
    log: [
      { tag: "PIPELINE",  level: "muted",  text: "Started | run_id=8c3f1a92" },
      { tag: "PLANNER",   level: "muted",  text: "Loaded 4 memory facts" },
      { tag: "PLANNER",   level: "info",   text: "Calling Gemini (attempt 1)..." },
      { tag: "PLANNER",   level: "info",   text: "Token usage | input=1840 | output=512" },
      { tag: "PLANNER",   level: "pass",   text: "Plan validated on attempt 1" },
      { tag: "PIPELINE",  level: "muted",  text: "Stage 1 complete: plan" },
      { tag: "CODER",     level: "info",   text: "Token usage | input=2104 | output=890" },
      { tag: "CODER",     level: "pass",   text: "Files changed: 1" },
      { tag: "PIPELINE",  level: "muted",  text: "Stage 2 complete: code" },
      { tag: "PATCH",     level: "pass",   text: "Backed up planner.py to backend/backups/" },
      { tag: "PATCH",     level: "pass",   text: "Applied 1 file. pre_hash=a3f1c2... post_hash=b4d8e1..." },
      { tag: "PIPELINE",  level: "muted",  text: "Stage 3 complete: patch" },
      { tag: "TESTER",    level: "pass",   text: "14 passed, 0 failed | 12.4 seconds" },
      { tag: "PIPELINE",  level: "muted",  text: "Stage 4 complete: test | passed=true" },
      { tag: "APPROVAL",  level: "info",   text: "Gate created | gate_id=ba8c4179" },
      { tag: "APPROVAL",  level: "info",   text: "Waiting for decision... (timeout in 30 minutes)" },
    ],
  },

  memory: [
    {
      id: "f1",
      content: "Pipeline always runs on port 8001. Port 8000 conflicts with target-repo Docker.",
      source: "DECISIONS.md",
      added_by: "satish",
      created_at: "2026-05-22T09:00:00Z",
      is_stale: false,
    },
    {
      id: "f2",
      content: "Never use time.sleep() in async context. Always await asyncio.sleep().",
      source: "DECISIONS.md",
      added_by: "satish",
      created_at: "2026-05-23T08:00:00Z",
      is_stale: false,
    },
    {
      id: "f3",
      content: "Coder never writes to disk directly. patch_applier owns all writes and backups.",
      source: "AGENTS.md",
      added_by: "satish",
      created_at: "2026-05-22T09:00:00Z",
      is_stale: false,
    },
    {
      id: "f4",
      content: "Use Gemini gemini-2.5-flash. Switch back to claude-sonnet-4-5 when key is available.",
      source: "AGENTS.md",
      added_by: "satish",
      created_at: "2026-05-22T09:00:00Z",
      is_stale: true,
    },
    {
      id: "f5",
      content: "Anthropic key is now available — switch PLANNER_MODEL and CODER_MODEL to claude-sonnet-4-5.",
      source: "manual",
      added_by: "satish",
      created_at: "2026-05-23T12:00:00Z",
      is_stale: false,
    },
  ],
};
