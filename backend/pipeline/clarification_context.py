"""
clarification_context.py
Stateless, signed clarification-context codec (PR #17L).

Purpose
-------
When ``/runs/chunked`` returns an ambiguous-file clarification (multiple indexed
README candidates, etc.), a follow-up reply like "yes 1" / "use README.md" only
means something *relative to the candidate list that was just shown*. This module
carries that candidate list (plus the original intent and project binding) across
the two requests as a single opaque, **integrity-protected** token — without any
DB row, schema change, or migration.

Design (see docs/decisions/clarification-selection-handoff.md, section 4)
-----------------------------------------------------------------
- The context is **not secret**; it only needs **integrity**. We therefore HMAC
  (SHA-256) sign a JSON payload rather than encrypt it. No Fernet here.
- The token is ``base64url(payload) + "." + base64url(signature)`` — URL-path
  safe (no ``+``/``/``/``=`` characters), suitable for ``/clarifications/{id}/``.
- Decoding **always verifies the signature before trusting the payload**.
  Tampered, malformed, expired, or unsupported-version tokens are reported, never
  silently accepted.

Signing key (no new required env var)
-------------------------------------
- If ``PIPEWRIGHT_ENCRYPTION_KEY`` is configured we derive a *stable* signing key
  from it (HKDF-style domain-separated SHA-256), so tokens survive restarts. We
  never sign with the raw encryption key.
- Otherwise we generate a **process-ephemeral** 32-byte key at import time. A
  server restart then invalidates outstanding clarification tokens, which is
  acceptable for a short-lived (default 30 min) clarification — the user simply
  re-submits the original request and gets a fresh one.

This module is pure: no DB, no filesystem, no repo index, no LLM, no routes. It is
not imported by any runtime route in #17L (only by tests); #17M wires it in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from backend.config.keys import settings

# Bump only on an incompatible payload-shape change. Decoding rejects any other
# version (VERSION_UNSUPPORTED) even when the signature is valid.
CLARIFICATION_CONTEXT_VERSION = 1

DEFAULT_TTL_MINUTES = 30

# Domain separation so this signing key can never collide with another HMAC use
# that happens to derive from the same configured secret.
_SIGNING_DOMAIN = b"pipewright.clarification.context.v1"


class ClarificationContext(BaseModel):
    """
    The disambiguation context carried between the clarification response and the
    follow-up selection request. Validated on construction.
    """

    model_config = ConfigDict(frozen=True)

    version: int = CLARIFICATION_CONTEXT_VERSION
    project_id: str
    original_feature_description: str
    alias: str | None = None
    candidates: list[str]
    recommended_path: str | None = None
    recommendation_strength: str | None = None
    created_at: datetime
    expires_at: datetime

    @field_validator("candidates")
    @classmethod
    def _candidates_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError(
                "clarification_context.py: candidates must not be empty"
            )
        return value

    @model_validator(mode="after")
    def _validate_rules(self) -> "ClarificationContext":
        if (
            self.recommended_path is not None
            and self.recommended_path not in self.candidates
        ):
            raise ValueError(
                "clarification_context.py: recommended_path must be one of "
                "candidates"
            )
        if self.expires_at <= self.created_at:
            raise ValueError(
                "clarification_context.py: expires_at must be after created_at"
            )
        return self


class ClarificationDecodeStatus(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    EXPIRED = "expired"
    VERSION_UNSUPPORTED = "version_unsupported"


class ClarificationDecodeResult(BaseModel):
    status: ClarificationDecodeStatus
    context: ClarificationContext | None = None
    reason: str | None = None

    @classmethod
    def ok(cls, context: ClarificationContext) -> "ClarificationDecodeResult":
        return cls(status=ClarificationDecodeStatus.OK, context=context)

    @classmethod
    def invalid(cls, reason: str) -> "ClarificationDecodeResult":
        return cls(status=ClarificationDecodeStatus.INVALID, reason=reason)

    @classmethod
    def expired(cls, reason: str = "token expired") -> "ClarificationDecodeResult":
        return cls(status=ClarificationDecodeStatus.EXPIRED, reason=reason)

    @classmethod
    def version_unsupported(cls, reason: str) -> "ClarificationDecodeResult":
        return cls(
            status=ClarificationDecodeStatus.VERSION_UNSUPPORTED,
            reason=reason,
        )


def _load_signing_key() -> bytes:
    """
    Derive a stable signing key from ``PIPEWRIGHT_ENCRYPTION_KEY`` when set
    (domain-separated, never the raw key), else a process-ephemeral random key.
    """
    configured = settings.pipewright_encryption_key
    if configured:
        return hashlib.sha256(
            _SIGNING_DOMAIN + configured.encode("utf-8")
        ).digest()
    return os.urandom(32)


# Resolved once at import. Process-ephemeral by default (restart invalidates
# outstanding tokens, which is acceptable for short-lived clarifications).
_SIGNING_KEY = _load_signing_key()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: bytes) -> bytes:
    return hmac.new(_SIGNING_KEY, payload, hashlib.sha256).digest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_clarification_context(
    *,
    project_id: str,
    original_feature_description: str,
    alias: str | None,
    candidates: Sequence[str],
    recommended_path: str | None,
    recommendation_strength: str | None,
    now: datetime | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> ClarificationContext:
    """
    Build a validated clarification context. Raises on invalid input (empty
    candidates, recommended_path not in candidates, non-positive ttl). Pure:
    reads no DB/FS/index.
    """
    if ttl_minutes <= 0:
        raise ValueError(
            "clarification_context.py: ttl_minutes must be positive"
        )
    created_at = now or _utc_now()
    return ClarificationContext(
        version=CLARIFICATION_CONTEXT_VERSION,
        project_id=project_id,
        original_feature_description=original_feature_description,
        alias=alias,
        candidates=list(candidates),
        recommended_path=recommended_path,
        recommendation_strength=recommendation_strength,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=ttl_minutes),
    )


def encode_clarification_context(context: ClarificationContext) -> str:
    """
    Serialize the context to JSON and return ``b64url(payload).b64url(sig)``.
    The exact serialized bytes are what get signed, so decode verifies against
    the same bytes (no re-serialization mismatch).
    """
    payload = context.model_dump_json().encode("utf-8")
    signature = _sign(payload)
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


def decode_clarification_context(
    token: str,
    *,
    now: datetime | None = None,
) -> ClarificationDecodeResult:
    """
    Verify and decode a clarification token. Verifies the HMAC signature *before*
    trusting the payload, then checks version, schema, and expiry. Never raises
    for an untrusted token; returns a typed result instead.
    """
    if not token or not isinstance(token, str):
        return ClarificationDecodeResult.invalid("empty token")

    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return ClarificationDecodeResult.invalid("malformed token structure")

    try:
        payload = _b64url_decode(parts[0])
        signature = _b64url_decode(parts[1])
    except Exception:
        return ClarificationDecodeResult.invalid("base64 decode failed")

    expected = _sign(payload)
    if not hmac.compare_digest(signature, expected):
        return ClarificationDecodeResult.invalid("signature mismatch")

    # Signature is valid: the payload is now trusted to be one we issued. Check
    # the declared version before binding it to the (possibly newer) schema.
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return ClarificationDecodeResult.invalid("payload is not valid JSON")

    if not isinstance(data, dict):
        return ClarificationDecodeResult.invalid("payload is not an object")

    if data.get("version") != CLARIFICATION_CONTEXT_VERSION:
        return ClarificationDecodeResult.version_unsupported(
            f"unsupported version: {data.get('version')!r}"
        )

    try:
        context = ClarificationContext.model_validate(data)
    except ValidationError as error:
        return ClarificationDecodeResult.invalid(f"schema validation failed: {error}")

    current = now or _utc_now()
    if current >= context.expires_at:
        return ClarificationDecodeResult.expired()

    return ClarificationDecodeResult.ok(context)
