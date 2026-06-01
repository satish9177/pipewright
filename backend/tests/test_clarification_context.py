"""
Tests for the signed clarification-context codec (PR #17L).

Pure unit tests. No DB, no FastAPI, no disk, no LLM. The signing key is the
module's process-ephemeral key, so encode/decode round-trips work in-process.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.pipeline.clarification_context import (
    CLARIFICATION_CONTEXT_VERSION,
    ClarificationContext,
    ClarificationDecodeStatus,
    create_clarification_context,
    decode_clarification_context,
    encode_clarification_context,
)

pytestmark = pytest.mark.unit


CANDIDATES = [
    "README.md",
    "docs/adr/README.md",
    "docs/architecture/README.md",
]


def _make(now=None, ttl_minutes=30):
    return create_clarification_context(
        project_id="proj-1",
        original_feature_description="add hello in the main readme",
        alias="readme",
        candidates=CANDIDATES,
        recommended_path="README.md",
        recommendation_strength="strong",
        now=now,
        ttl_minutes=ttl_minutes,
    )


# 1. Round trip -------------------------------------------------------------

def test_round_trip_preserves_fields():
    context = _make()
    token = encode_clarification_context(context)

    result = decode_clarification_context(token)

    assert result.status is ClarificationDecodeStatus.OK
    decoded = result.context
    assert decoded is not None
    assert decoded.version == CLARIFICATION_CONTEXT_VERSION
    assert decoded.project_id == "proj-1"
    assert decoded.original_feature_description == "add hello in the main readme"
    assert decoded.alias == "readme"
    assert decoded.candidates == CANDIDATES
    assert decoded.recommended_path == "README.md"
    assert decoded.recommendation_strength == "strong"
    assert decoded.created_at == context.created_at
    assert decoded.expires_at == context.expires_at


def test_token_is_url_path_safe():
    token = encode_clarification_context(_make())
    # No characters that would need escaping in a URL path segment.
    for char in "+/= ":
        assert char not in token
    assert token.count(".") == 1


# 2. Tampering --------------------------------------------------------------

def test_tampered_payload_is_rejected():
    token = encode_clarification_context(_make())
    payload, signature = token.split(".")
    # Flip one character in the payload portion.
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    tampered = f"{flipped}.{signature}"

    result = decode_clarification_context(tampered)

    assert result.status is ClarificationDecodeStatus.INVALID


def test_tampered_signature_is_rejected():
    token = encode_clarification_context(_make())
    payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = f"{payload}.{flipped}"

    result = decode_clarification_context(tampered)

    assert result.status is ClarificationDecodeStatus.INVALID


@pytest.mark.parametrize(
    "token",
    ["", "noseparator", "a.b.c", ".", "abc.", ".abc", "!!!.???"],
)
def test_malformed_tokens_are_rejected(token):
    result = decode_clarification_context(token)
    assert result.status is ClarificationDecodeStatus.INVALID


# 3. Expiry -----------------------------------------------------------------

def test_expired_token_reports_expired():
    created = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    token = encode_clarification_context(_make(now=created, ttl_minutes=30))

    after_expiry = created + timedelta(minutes=31)
    result = decode_clarification_context(token, now=after_expiry)

    assert result.status is ClarificationDecodeStatus.EXPIRED


def test_token_valid_before_expiry():
    created = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    token = encode_clarification_context(_make(now=created, ttl_minutes=30))

    just_before = created + timedelta(minutes=29)
    result = decode_clarification_context(token, now=just_before)

    assert result.status is ClarificationDecodeStatus.OK


# 4. Version mismatch -------------------------------------------------------

def test_unsupported_version_is_rejected():
    # A context with an unsupported version is still signed with the module key,
    # so the signature is valid but decode must reject on version.
    base = _make()
    bumped = base.model_copy(
        update={"version": CLARIFICATION_CONTEXT_VERSION + 999}
    )
    token = encode_clarification_context(bumped)

    result = decode_clarification_context(token)

    assert result.status is ClarificationDecodeStatus.VERSION_UNSUPPORTED


# 5. Validation -------------------------------------------------------------

def test_empty_candidates_rejected_at_creation():
    with pytest.raises(ValidationError):
        create_clarification_context(
            project_id="proj-1",
            original_feature_description="x",
            alias="readme",
            candidates=[],
            recommended_path=None,
            recommendation_strength=None,
        )


def test_recommended_path_outside_candidates_rejected():
    with pytest.raises(ValidationError):
        create_clarification_context(
            project_id="proj-1",
            original_feature_description="x",
            alias="readme",
            candidates=CANDIDATES,
            recommended_path="not/in/list.md",
            recommendation_strength="strong",
        )


def test_expires_at_before_created_at_rejected():
    created = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        ClarificationContext(
            version=CLARIFICATION_CONTEXT_VERSION,
            project_id="proj-1",
            original_feature_description="x",
            alias=None,
            candidates=CANDIDATES,
            recommended_path=None,
            recommendation_strength=None,
            created_at=created,
            expires_at=created - timedelta(minutes=5),
        )


def test_non_positive_ttl_rejected():
    with pytest.raises(ValueError):
        create_clarification_context(
            project_id="proj-1",
            original_feature_description="x",
            alias=None,
            candidates=CANDIDATES,
            recommended_path=None,
            recommendation_strength=None,
            ttl_minutes=0,
        )
