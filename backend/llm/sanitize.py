"""
Sanitization helpers for provider boundary logs and errors.
"""

import re

REDACTION = "[REDACTED]"

_GEMINI_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[0-9A-Za-z_\-]{16,}")
_OPENAI_KEY_RE = re.compile(r"sk-[0-9A-Za-z_\-]{20,}")
_BEARER_RE = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"
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
    sanitized = _GEMINI_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _ANTHROPIC_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _OPENAI_KEY_RE.sub(REDACTION, sanitized)
    sanitized = _BEARER_RE.sub(f"Bearer {REDACTION}", sanitized)
    sanitized = _SECRET_CONTEXT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}",
        sanitized,
    )
    return sanitized
