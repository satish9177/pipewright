"""
test_policy.py
Regression guard for the §8b policy module: pure relocation, zero behavior
change. Pins every relocated constant's value, asserts the stage modules read
from policy (one source of truth), and asserts the dead per-stage model
constants were deleted outright — role_config is the only model authority.
"""

import importlib

import pytest

from backend.pipeline import (
    coder,
    patch_dry_run,
    planner,
    policy,
    reviewer,
    tester,
    triage,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def reload_prompt_cache_policy(monkeypatch):
    def reload_with(value: str | None):
        if value is None:
            monkeypatch.delenv(policy.PROMPT_CACHE_ENV, raising=False)
        else:
            monkeypatch.setenv(policy.PROMPT_CACHE_ENV, value)
        return importlib.reload(policy)

    yield reload_with

    monkeypatch.delenv(policy.PROMPT_CACHE_ENV, raising=False)
    importlib.reload(policy)


def test_policy_values_are_unchanged_by_relocation():
    assert policy.TESTER_TIMEOUT_SECONDS == 300
    assert policy.MAX_OUTPUT_CHARS == 10000
    assert policy.SCOPED_VERIFICATION_ENABLED is False
    assert policy.MAX_FILE_LINES == 200
    assert policy.LARGE_FILE_CONTEXT_LINE_CAP == 1500
    assert policy.REVIEWER_MAX_DIFF_CHARS == 6000
    assert policy.REVIEW_ACK_REQUIRED_SEVERITY == "high"
    assert policy.REVIEW_ACK_REQUIRED_CATEGORIES == frozenset({
        "requirement_mismatch",
        "security",
    })
    assert policy.MERGED_PROFILE_SAMPLE_PCT == 50
    assert policy.PROMPT_CACHE_ENV == "PIPEWRIGHT_PROMPT_CACHE_ENABLED"
    assert "schema.sql" in policy.TRIVIAL_PROFILE_DENYLIST_PATTERNS
    assert "requirements*.txt" in policy.TRIVIAL_PROFILE_DENYLIST_PATTERNS
    assert policy.REPO_REALITY_SIGNAL_DIMENSIONS == frozenset({
        "db_engine",
        "backend_framework",
        "frontend_framework",
        "test_runner",
        "migration_tool",
        "package_manager",
    })


def test_prompt_cache_env_unset_defaults_false(reload_prompt_cache_policy):
    reloaded = reload_prompt_cache_policy(None)

    assert reloaded.PROMPT_CACHE_ENABLED is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", " On "])
def test_prompt_cache_env_truthy_values_enable_flag(
    reload_prompt_cache_policy,
    value,
):
    reloaded = reload_prompt_cache_policy(value)

    assert reloaded.PROMPT_CACHE_ENABLED is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe", "treu"])
def test_prompt_cache_env_falsey_or_invalid_values_disable_flag(
    reload_prompt_cache_policy,
    value,
):
    reloaded = reload_prompt_cache_policy(value)

    assert reloaded.PROMPT_CACHE_ENABLED is False


def test_stages_read_constants_from_policy():
    assert tester.TESTER_TIMEOUT_SECONDS == policy.TESTER_TIMEOUT_SECONDS
    assert tester.MAX_OUTPUT_CHARS == policy.MAX_OUTPUT_CHARS
    assert coder.MAX_FILE_LINES == policy.MAX_FILE_LINES
    assert coder.LARGE_FILE_CONTEXT_LINE_CAP == policy.LARGE_FILE_CONTEXT_LINE_CAP
    assert reviewer.REVIEWER_MAX_DIFF_CHARS == policy.REVIEWER_MAX_DIFF_CHARS
    # Previously a hand-mirrored copy of the coder's cap; now single-sourced.
    assert patch_dry_run.MAX_MODIFY_FILE_LINES == policy.MAX_FILE_LINES


def test_dead_model_constants_are_deleted():
    # These lied: complete_for_role always overwrites request.model from
    # role_config. Deleted outright, never aliased.
    assert not hasattr(triage, "TRIAGE_MODEL")
    assert not hasattr(planner, "PLANNER_MODEL")
    assert not hasattr(coder, "CODER_MODEL")


def test_policy_module_stays_pure_constants():
    # No backend imports: any module (including cycle-free pure layers like
    # patch_dry_run) must be able to read policy safely.
    import inspect

    source = inspect.getsource(policy)
    assert "import backend" not in source
    assert "from backend" not in source
