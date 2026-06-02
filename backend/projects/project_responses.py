"""
project_responses.py
Helpers for safe project API response payloads.
"""

from backend.pipeline.test_command_quality import classify_test_command


def sanitize_project_response(project: dict) -> dict:
    """
    Return a project payload safe for API responses.
    Internal project records may contain github_token, but API responses must
    only expose whether a token is configured.

    Also attaches a deterministic, computed-on-read classification of the
    project's test command (#23A): ``test_command_quality`` (weak / likely_test
    / unknown) and ``test_command_quality_reason``. This is derived purely from
    the stored ``test_command`` string — nothing is stored, no schema changes,
    and execution/checkpoint behavior is unaffected.
    """
    safe_project = dict(project)
    github_token = safe_project.pop("github_token", None)
    safe_project["has_github_token"] = bool(github_token)

    quality = classify_test_command(safe_project.get("test_command") or "")
    safe_project["test_command_quality"] = quality.quality.value
    safe_project["test_command_quality_reason"] = quality.reason

    return safe_project


def sanitize_project_list_response(projects: list[dict]) -> list[dict]:
    return [sanitize_project_response(project) for project in projects]
