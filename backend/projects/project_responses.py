"""
project_responses.py
Helpers for safe project API response payloads.
"""


def sanitize_project_response(project: dict) -> dict:
    """
    Return a project payload safe for API responses.
    Internal project records may contain github_token, but API responses must
    only expose whether a token is configured.
    """
    safe_project = dict(project)
    github_token = safe_project.pop("github_token", None)
    safe_project["has_github_token"] = bool(github_token)
    return safe_project


def sanitize_project_list_response(projects: list[dict]) -> list[dict]:
    return [sanitize_project_response(project) for project in projects]
