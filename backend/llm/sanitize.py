"""
Sanitization helpers for provider boundary logs and errors.
"""

import re

REDACTION = "[REDACTED]"

_GEMINI_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[0-9A-Za-z_\-]{16,}")
_OPENAI_KEY_RE = re.compile(r"sk-[0-9A-Za-z_\-]{20,}")
# GitHub fine-grained PATs (github_pat_...) contain underscores; classic and
# OAuth/app tokens are ghp_/gho_/ghs_/ghu_/ghr_ followed by base62. Conservative
# lengths (20+) avoid matching ordinary words while catching real tokens.
_GITHUB_PAT_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,}")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}\b")
_BEARER_RE = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"
)
# Credentials embedded in a URL userinfo (scheme://user:token@host or
# scheme://token@host). Redacts the whole userinfo and keeps scheme/host/path.
# Requires "://" so SSH remotes (git@github.com:owner/repo) are left intact.
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@"
)
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)\b(key|token|authorization|credential|secret)\b"
    r"(\s*[:=]\s*|\s+)"
    r"([A-Za-z0-9._~+/=\-]{32,})"
)


def sanitize_for_log(value: str) -> str:
    """
    Redact provider/API secrets without hiding ordinary commit hashes.
    """
    if value is None:
        return ""

    sanitized = str(value)
    # Redact URL-embedded credentials first so the host/path stay readable
    # (https://[REDACTED]@github.com/owner/repo.git).
    sanitized = _URL_CREDENTIALS_RE.sub(
        lambda match: f"{match.group('scheme')}{REDACTION}@",
        sanitized,
    )
    sanitized = _GEMINI_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _ANTHROPIC_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _OPENAI_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _GITHUB_PAT_RE.sub(REDACTION, sanitized)
    sanitized = _GITHUB_TOKEN_RE.sub(REDACTION, sanitized)
    sanitized = _BEARER_RE.sub(f"Bearer {REDACTION}", sanitized)
    sanitized = _SECRET_CONTEXT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}",
        sanitized,
    )
    return sanitized
